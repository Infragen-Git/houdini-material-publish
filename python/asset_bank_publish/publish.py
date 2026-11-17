"""Material publishing: turns the layer saved after a Layer Break into what the bank expects, then uploads.

Pure pxr + standard library (no hou), so it can be tested outside Houdini.

Asset mode   -> {Asset}_mtl.usda + textures/  (POST /api/assets/{Asset}/material)
    - every Material outside /{Asset} (Houdini's /materials) moves to /{Asset}/looks/<name>, paths fixed
    - bindings are pushed down to each mesh (and bound GeomSubset): a binding on an ancestor such as
      `geo` would lose against the shared code rules, which sit on `geo` too
    - textures are copied next to the layer, paths made relative (./textures/...)
Shared mode  -> one {CODE}.usda (+ textures/) per material, defaultPrim = the material
    (POST /api/materials/{CODE}). Code: the bank already maps the material's name to a code, or the
    name is CODE_Name (GV_Galvanized -> GV, Galvanized).
"""
import glob
import json
import os
import re
import shutil
import uuid
import urllib.error
import urllib.request

from pxr import Sdf, Usd, UsdShade

from asset_bank import bank_client as bc

CODE_NAME = re.compile(r"^([A-Z]{2,3})_([A-Za-z_][A-Za-z0-9_]*)$")
DROP_ROOTS = ("HoudiniLayerInfo",)
BINDING = "material:binding"


class PublishError(Exception):
    pass


class Report:
    def __init__(self):
        self.lines, self.warnings = [], []

    def add(self, text):
        self.lines.append(text)

    def warn(self, text):
        self.warnings.append(text)

    def text(self):
        out = list(self.lines)
        if self.warnings:
            out += ["", "Warnings:"] + ["  - " + w for w in self.warnings]
        return "\n".join(out)


# ---------------------------------------------------------------- layer helpers
def _all_specs(layer):
    specs = []
    layer.Traverse(Sdf.Path.absoluteRootPath, specs.append)
    return specs


def _materials(layer, under=Sdf.Path.absoluteRootPath):
    """Paths of Material prim specs (not nested in another material) under `under`."""
    found = []
    for path in _all_specs(layer):
        if not path.IsPrimPath() or not path.HasPrefix(under):
            continue
        spec = layer.GetPrimAtPath(path)
        if spec and spec.typeName == "Material" and not any(path.HasPrefix(m) for m in found):
            found.append(path)
    return found


def _remap_paths(layer, mapping, keep=lambda p: True):
    """Rewrites every path in the layer (connections, relationship targets, inherits, specializes,
    internal references) with mapping {old prefix: new prefix}; drops paths for which keep() is False."""
    def fix(p):
        if p.isEmpty:
            return p
        for old, new in mapping.items():
            if p.HasPrefix(old):
                p = p.ReplacePrefix(old, new)
                break
        return p if keep(p.GetAbsoluteRootOrPrimPath()) else None

    for path in _all_specs(layer):
        if path.IsPropertyPath():
            spec = layer.GetPropertyAtPath(path)
            if isinstance(spec, Sdf.AttributeSpec):
                spec.connectionPathList.ModifyItemEdits(fix)
            elif isinstance(spec, Sdf.RelationshipSpec):
                spec.targetPathList.ModifyItemEdits(fix)
        elif path.IsPrimPath():
            spec = layer.GetPrimAtPath(path)
            spec.inheritPathList.ModifyItemEdits(fix)
            spec.specializesList.ModifyItemEdits(fix)


def _move(layer, old, new):
    parent = new.GetParentPath()
    if not layer.GetPrimAtPath(parent):
        Sdf.CreatePrimInLayer(layer, parent)
    if not Sdf.CopySpec(layer, old, layer, new):
        raise PublishError("could not move {} to {}".format(old, new))


def _remove_root(layer, name):
    spec = layer.GetPrimAtPath("/" + name)
    if spec:
        del layer.rootPrims[name]


def _unique(name, taken):
    out, i = name, 1
    while out in taken:
        i += 1
        out = "{}_{}".format(name, i)
    taken.add(out)
    return out


# ---------------------------------------------------------------- textures
def _asset_values(layer):
    """(spec, key, AssetPath) for every asset-valued attribute default and time sample."""
    for path in _all_specs(layer):
        if not path.IsPropertyPath():
            continue
        spec = layer.GetPropertyAtPath(path)
        if not isinstance(spec, Sdf.AttributeSpec):
            continue
        if isinstance(spec.default, Sdf.AssetPath):
            yield spec, None, spec.default
        for t in layer.ListTimeSamplesForPath(path):
            v = layer.QueryTimeSample(path, t)
            if isinstance(v, Sdf.AssetPath):
                yield spec, t, v


def copy_textures(layer, anchor_dir, out_dir, report):
    """Copies every file an asset attribute points to into out_dir/textures and makes the path relative.
    <UDIM> tiles are all copied. Missing files are reported and left as they are."""
    done, used = {}, set()
    for spec, t, value in list(_asset_values(layer)):
        raw = value.path
        if not raw or raw.startswith(("./textures/", "textures/")) or "://" in raw:
            continue
        src = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(anchor_dir, raw))
        if "<UDIM>" in src:
            files = glob.glob(src.replace("<UDIM>", "[1-9][0-9][0-9][0-9]"))
        else:
            files = [src] if os.path.isfile(src) else []
        if not files:
            report.warn("texture not found, path kept: " + raw)
            continue
        if src not in done:
            prefix, n = "", 1  # two different files with the same name: prefix the later ones
            while (prefix + os.path.basename(src)).lower() in used:
                n += 1
                prefix = "{}_".format(n)
            used.add((prefix + os.path.basename(src)).lower())
            os.makedirs(os.path.join(out_dir, "textures"), exist_ok=True)
            for f in files:  # one file, or every <UDIM> tile
                shutil.copy2(f, os.path.join(out_dir, "textures", prefix + os.path.basename(f)))
            done[src] = "./textures/" + prefix + os.path.basename(src)
            report.add("texture  {}  ->  {}".format(raw, done[src]))
        new = Sdf.AssetPath(done[src])
        if t is None:
            spec.default = new
        else:
            spec.layer.SetTimeSample(spec.path, t, new)
    return len(done)


def _check_external(layer, report):
    for path in _all_specs(layer):
        if path.IsPrimPath():
            spec = layer.GetPrimAtPath(path)
            for ref in list(spec.referenceList.GetAddedOrExplicitItems()) + list(spec.payloadList.GetAddedOrExplicitItems()):
                if ref.assetPath:
                    report.warn("{} references another file ({}): it will not be in the bank".format(path, ref.assetPath))


def _clean_metadata(layer, default_prim):
    layer.defaultPrim = default_prim
    for key in ("startTimeCode", "endTimeCode", "framesPerSecond", "timeCodesPerSecond"):
        if layer.pseudoRoot.HasInfo(key):
            layer.pseudoRoot.ClearInfo(key)
    if layer.pseudoRoot.HasInfo("customLayerData"):
        layer.pseudoRoot.ClearInfo("customLayerData")
    for name in DROP_ROOTS:
        _remove_root(layer, name)


# ---------------------------------------------------------------- preview surfaces
# MaterialX surface -> UsdPreviewSurface inputs: (mtlx input, preview input, weight input or None)
SURFACES = {
    "ND_open_pbr_surface_surfaceshader": [("base_color", "diffuseColor", "base_weight"),
                                          ("base_metalness", "metallic", None),
                                          ("specular_roughness", "roughness", None)],
    "ND_standard_surface_surfaceshader": [("base_color", "diffuseColor", "base"),
                                          ("metalness", "metallic", None),
                                          ("specular_roughness", "roughness", None)],
}
DEFAULTS = {"base_color": (0.8, 0.8, 0.8), "base_weight": 1.0, "base": 1.0, "base_metalness": 0.0,
            "metalness": 0.0}  # node defaults; specular_roughness depends on the surface (see add_previews)


def add_previews(layer, report):
    """Gives every Material without a UsdPreviewSurface output (OpenPBR has no automatic one in Houdini)
    a preview built from its MaterialX surface's constant values, so InfraGen, Blender, usdview and the
    web page see its colour. Connected inputs (textures, ramps) fall back to the node default."""
    for mat in _materials(layer):
        spec = layer.GetPrimAtPath(mat)
        if "outputs:surface" in spec.properties:
            continue
        shaders = [c for c in spec.nameChildren if c.typeName == "Shader" and "info:id" in c.properties]
        surf = next((c for c in shaders if c.properties["info:id"].default in SURFACES), None)
        if surf is None:
            report.warn("{}: no preview surface and no known MaterialX surface: grey in InfraGen".format(mat))
            continue
        kind = surf.properties["info:id"].default
        defaults = dict(DEFAULTS, specular_roughness=0.3 if "open_pbr" in kind else 0.2)

        def value(name):
            prop = surf.properties.get("inputs:" + name)
            v = prop.default if prop is not None and prop.HasDefaultValue() else None
            return defaults[name] if v is None else v

        prev = Sdf.PrimSpec(spec, "preview_surface", Sdf.SpecifierDef, "Shader")
        Sdf.AttributeSpec(prev, "info:id", Sdf.ValueTypeNames.Token, variability=Sdf.VariabilityUniform).default = "UsdPreviewSurface"
        texts = []
        for src, dst, weight in SURFACES[kind]:
            v = value(src)
            if dst == "diffuseColor":
                w = float(value(weight)) if weight else 1.0
                v = tuple(float(c) * w for c in v)
                Sdf.AttributeSpec(prev, "inputs:diffuseColor", Sdf.ValueTypeNames.Color3f).default = v
                texts.append("colour ({:.2f}, {:.2f}, {:.2f})".format(*v))
            else:
                Sdf.AttributeSpec(prev, "inputs:" + dst, Sdf.ValueTypeNames.Float).default = float(v)
                texts.append("{} {:.2f}".format(dst, float(v)))
        out = Sdf.AttributeSpec(prev, "outputs:surface", Sdf.ValueTypeNames.Token)
        mout = Sdf.AttributeSpec(spec, "outputs:surface", Sdf.ValueTypeNames.Token)
        mout.connectionPathList.explicitItems = [out.path]
        report.add("preview surface added to {}: {}".format(mat.name, ", ".join(texts)))


# ---------------------------------------------------------------- asset mode
def _push_bindings(layer, name, geo_layer, report):
    """Replaces the layer's bindings by direct bindings on each mesh / bound GeomSubset, computed on
    geometry + this layer only (the shared rules are not composed in)."""
    root = Sdf.Layer.CreateAnonymous(".usda")
    root.subLayerPaths = [layer.identifier, geo_layer]
    stage = Usd.Stage.Open(root, Usd.Stage.LoadAll)
    asset = stage.GetPrimAtPath("/" + name)
    if not asset:
        raise PublishError("/{} not found in the geometry layer {}".format(name, geo_layer))
    bound = {}
    for prim in Usd.PrimRange(asset):
        if prim.GetTypeName() not in ("Mesh", "GeomSubset"):
            continue
        if prim.GetTypeName() == "GeomSubset" and not prim.GetAttribute("familyName").Get() == "materialBind":
            continue
        mat, rel = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if mat:
            bound[prim.GetPath()] = mat.GetPath()
    # remove every binding authored in the layer, then author the direct ones
    purposes = set()
    for path in _all_specs(layer):
        if path.IsPropertyPath() and path.name.startswith(BINDING):
            prop = path.name
            if prop != BINDING and not prop.startswith(BINDING + ":collection"):
                purposes.add(prop)
            owner = layer.GetPrimAtPath(path.GetPrimPath())
            owner.RemoveProperty(owner.properties[prop])
    if purposes:
        report.warn("purpose-specific bindings ({}) were folded into the all-purpose one".format(", ".join(sorted(purposes))))
    for path, mat in sorted(bound.items()):
        spec = layer.GetPrimAtPath(path) or Sdf.CreatePrimInLayer(layer, path)
        rel = Sdf.RelationshipSpec(spec, BINDING, custom=False)
        rel.targetPathList.explicitItems = [mat]
        schemas = spec.GetInfo("apiSchemas") if spec.HasInfo("apiSchemas") else Sdf.TokenListOp()
        if "MaterialBindingAPI" not in schemas.GetAddedOrExplicitItems():
            schemas.prependedItems = list(schemas.prependedItems) + ["MaterialBindingAPI"]
            spec.SetInfo("apiSchemas", schemas)
    meshes = sum(1 for p in Usd.PrimRange(asset) if p.GetTypeName() == "Mesh")
    report.add("bindings: {} of {} meshes take an asset material (the others keep the shared code rules)".format(
        len([p for p in bound if stage.GetPrimAtPath(p).GetTypeName() == "Mesh"]), meshes))
    used = sorted(set(bound.values()))
    for m in used:
        report.add("  {}  <- {} prims".format(m, sum(1 for v in bound.values() if v == m)))
    return used


def prepare_asset(src_path, name, geo_layer, out_dir):
    """Builds out_dir/{name}_mtl.usda (+ textures) from the layer saved after the Layer Break.
    Returns a Report. Raises PublishError when there is nothing to publish."""
    report = Report()
    src = Sdf.Layer.FindOrOpen(src_path)
    if src is None:
        raise PublishError("cannot open " + src_path)
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.TransferContent(src)
    root = Sdf.Path("/" + name)

    mapping, taken = {}, set()
    looks = root.AppendChild("looks")
    if layer.GetPrimAtPath(looks):
        taken.update(c.name for c in layer.GetPrimAtPath(looks).nameChildren)
    # every material goes to /{name}/looks: outside the asset it would be lost when the asset is
    # referenced, and /{name}/mtl is where the shared code materials compose (names could merge)
    elsewhere = [m for m in _materials(layer) if not m.HasPrefix(looks)]
    for m in elsewhere:
        mapping[m] = looks.AppendChild(_unique(m.name, taken))
    if mapping:
        if not layer.GetPrimAtPath(root):
            Sdf.CreatePrimInLayer(layer, root)
        if not layer.GetPrimAtPath(looks):
            Sdf.PrimSpec(layer.GetPrimAtPath(root), "looks", Sdf.SpecifierDef, "Scope")
        for old, new in mapping.items():
            _move(layer, old, new)
            report.add("material {}  ->  {}".format(old, new))
            if old.HasPrefix(root):  # moved inside the asset: remove the old spec, then empty scopes
                parent = layer.GetPrimAtPath(old.GetParentPath())
                del parent.nameChildren[old.name]
                while parent and parent.path != root and not parent.nameChildren and not parent.properties:
                    grand = layer.GetPrimAtPath(parent.path.GetParentPath())
                    del grand.nameChildren[parent.name]
                    parent = grand
    _remap_paths(layer, mapping, keep=lambda p: p.HasPrefix(root))
    for spec in list(layer.rootPrims):
        if spec.name == name:
            continue
        if spec.name not in DROP_ROOTS and not spec.name.startswith("__class"):
            extra = [p for p in _all_specs(layer) if p.HasPrefix(spec.path) and p != spec.path
                     and not any(p.HasPrefix(m) for m in mapping)]
            if extra:
                report.warn("{} is outside /{}: dropped ({})".format(spec.path, name, ", ".join(map(str, extra[:3]))))
        _remove_root(layer, spec.name)

    if not _materials(layer, root):
        raise PublishError("no Material after the Layer Break: nothing to publish")
    add_previews(layer, report)
    _clean_metadata(layer, name)
    _check_external(layer, report)
    used = _push_bindings(layer, name, geo_layer, report)
    if not used:
        report.warn("no mesh is bound to a material of the asset")
    os.makedirs(out_dir, exist_ok=True)
    copy_textures(layer, os.path.dirname(src.realPath), out_dir, report)
    out = os.path.join(out_dir, name + "_mtl.usda")
    layer.Export(out)
    report.add("written " + out)
    return report


# ---------------------------------------------------------------- shared mode
def plan_codes(src_path, known):
    """[(material path, code, name)] for every Material of the layer. known: {name: code} from the bank.
    Raises PublishError listing materials whose code cannot be told."""
    src = Sdf.Layer.FindOrOpen(src_path)
    plan, bad = [], []
    for m in _materials(src):
        match = CODE_NAME.match(m.name)
        if m.name in known:
            plan.append((m, known[m.name], m.name))
        elif match:
            plan.append((m, match.group(1), match.group(2)))
        else:
            bad.append(m.name)
    if bad:
        raise PublishError("cannot tell the code of: {}. Name them CODE_Name (GV_Galvanized) or use a name the "
                           "bank already has ({}).".format(", ".join(bad), ", ".join(sorted(known)) or "none yet"))
    codes = [c for _, c, _ in plan]
    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        raise PublishError("several materials for code {}".format(", ".join(dup)))
    if not plan:
        raise PublishError("no Material after the Layer Break: nothing to publish")
    return plan


def prepare_code(src_path, mat_path, code, name, out_dir):
    """out_dir/{code}.usda (+ textures): the material at mat_path, as /{name}. Returns a Report."""
    report = Report()
    src = Sdf.Layer.FindOrOpen(src_path)
    layer = Sdf.Layer.CreateAnonymous(".usda")
    new = Sdf.Path("/" + name)
    Sdf.PrimSpec(layer, name, Sdf.SpecifierDef, "Material")
    if not Sdf.CopySpec(src, Sdf.Path(str(mat_path)), layer, new):
        raise PublishError("could not copy " + str(mat_path))
    _remap_paths(layer, {Sdf.Path(str(mat_path)): new}, keep=lambda p: p.HasPrefix(new))
    add_previews(layer, report)
    _clean_metadata(layer, name)
    os.makedirs(out_dir, exist_ok=True)
    copy_textures(layer, os.path.dirname(src.realPath), out_dir, report)
    out = os.path.join(out_dir, code + ".usda")
    layer.Export(out)
    report.add("{}  {}  ->  {}".format(code, name, out))
    return report


# ---------------------------------------------------------------- upload
def _multipart(fields, files):
    boundary = "----assetbank" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, k, v).encode()
    for rel, path in files:
        with open(path, "rb") as f:
            data = f.read()
        body += ("--{}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").format(boundary, rel).encode()
        body += data + b"\r\n"
    body += "--{}--\r\n".format(boundary).encode()
    return bytes(body), "multipart/form-data; boundary=" + boundary


def _files(folder):
    out = []
    for dirpath, _, names in os.walk(folder):
        for n in sorted(names):
            p = os.path.join(dirpath, n)
            out.append((os.path.relpath(p, folder).replace("\\", "/"), p))
    return out


def upload(base, route, folder, fields, token):
    if not token:
        raise PublishError("no publishing token (Asset Bank panel > Settings, or ASSET_BANK_TOKEN)")
    files = _files(folder)
    total = sum(os.path.getsize(p) for _, p in files)
    if total > 95 * 1024 * 1024:
        raise PublishError("{:.0f} MB to send: over the 100 MB a publish can carry (Cloudflare)".format(total / 1e6))
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(bc._url(base, route), data=body, method="POST", headers={
        "Content-Type": ctype, "Authorization": "Bearer " + token, "User-Agent": bc.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = e.reason
        raise PublishError("{} {}: {}".format(e.code, route, detail)) from None
    except urllib.error.URLError as e:
        raise PublishError("server unreachable: {}".format(e.reason)) from None
