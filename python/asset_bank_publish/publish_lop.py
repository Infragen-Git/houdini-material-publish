"""The "Bank Publish Material" LOP (asset_bank::bank_publish::1.0): saves what comes after the Layer Break
with its inner USD ROP, prepares it (asset_bank.publish) and uploads it. Passes its input through."""
import os
import shutil
import tempfile

import hou

from asset_bank import bank_client as bc
from asset_bank import core

from . import publish

NODE_TYPE = "asset_bank::bank_publish::1.0"
BANK_ASSET = "asset_bank::bank_asset::1.0"


def detect_asset(node):
    """The asset being look-developed: the nearest Bank Asset LOP upstream, else the stage's defaultPrim."""
    for n in node.inputAncestors():
        if n.type().name() == BANK_ASSET and n.evalParm("asset"):
            return n.evalParm("asset")
    src = node.input(0)
    if src is not None:
        prim = src.stage().GetDefaultPrim()
        if prim:
            return prim.GetName()
    return ""


def _set_report(node, text):
    node.parm("report").set(text)


def _save_layer(node, folder):
    """Saves the layers after the Layer Break into one file with the inner USD ROP."""
    if node.input(0) is None:
        raise publish.PublishError("connect the material network (after a Layer Break) to the input")
    if not node.input(0).layersAboveLayerBreak():
        raise publish.PublishError("no Layer Break upstream: put one after the Bank Asset node, so only the "
                                   "material work is published")
    path = os.path.join(folder, "after_layer_break.usda").replace("\\", "/")
    node.parm("savepath").set(path)
    rop = node.node("save")
    rop.render()
    if rop.errors() or not os.path.isfile(path):
        raise publish.PublishError("saving the layer failed: {}".format(" ".join(rop.errors())))
    return path


def _prepare(node, work):
    """Returns (report text, list of uploads [(route, folder, fields)], what to refresh after)."""
    src = _save_layer(node, work)
    description = node.evalParm("description")
    user = os.environ.get("USERNAME", "")
    source = hou.hipFile.path()
    if node.evalParm("mode") == "asset":
        name = node.evalParm("asset")
        if not name:
            raise publish.PublishError("no asset: set Asset, or start the network from a Bank Asset node")
        latest = os.path.join(core.library(), name, "geometry", "latest")
        geo = bc.version_file(latest) if os.path.isdir(latest) else None
        if not geo:
            raise publish.PublishError("{} is not in the local library ({}): import it with the Asset Bank first"
                                       .format(name, core.library()))
        out = os.path.join(work, name)
        rep = publish.prepare_asset(src, name, geo, out)
        fields = {"user": user, "description": description, "source": source}
        return rep.text(), [("api/assets/{}/material".format(name), out, fields)], ("asset", name)
    listing = bc.json.loads(bc._get(bc._url(core.url(), "api/materials"), timeout=30))
    known = {m["name"]: m["code"] for m in listing.get("materials", [])}
    plan = publish.plan_codes(src, known)
    texts, uploads = [], []
    for mat, code, name in plan:
        out = os.path.join(work, code)
        texts.append(publish.prepare_code(src, mat, code, name, out).text())
        uploads.append(("api/materials/" + code, out, {"name": name, "user": user, "description": description,
                                                       "source": source}))
    new = [c for _, c, n in plan if n not in known]
    if new:
        texts.append("new code(s): " + ", ".join(new))
    return "\n".join(texts), uploads, ("shared", None)


def check(node):
    """Button: prepares without sending and shows what would be published."""
    work = tempfile.mkdtemp(prefix="bank-publish-")
    try:
        text, _, _ = _prepare(node, work)
        _set_report(node, "CHECK (nothing sent)\n\n" + text)
    except (publish.PublishError, bc.BankError) as e:
        _set_report(node, "ERROR: {}".format(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run(node, confirm=True):
    """Button: prepares, asks, uploads, then brings the result back into the local library."""
    work = tempfile.mkdtemp(prefix="bank-publish-")
    try:
        with hou.InterruptableOperation("Asset Bank: publishing materials", open_interrupt_dialog=True):
            text, uploads, (kind, name) = _prepare(node, work)
            if confirm and hou.isUIAvailable():
                where = "the asset {}".format(name) if kind == "asset" else "the shared library"
                if hou.ui.displayMessage("Publish to {}?".format(where), buttons=("Publish", "Cancel"),
                                         default_choice=0, close_choice=1, details=text,
                                         title="Asset Bank") != 0:
                    _set_report(node, "Cancelled\n\n" + text)
                    return None
            results = []
            for route, folder, fields in uploads:
                res = publish.upload(core.url(), route, folder, fields, core.token())
                label = res.get("code") or res.get("name")
                results.append("{} {}: {}".format(label, res["published"], "published" if res["created"]
                                                  else "unchanged (identical to the latest version)"))
            core.bank_assets(refresh=True)
            if kind == "asset":
                core.ensure(name)
            core.pull_materials()
            _reload_bank_assets(node)
        _set_report(node, "PUBLISHED\n" + "\n".join(results) + "\n\n" + text)
        return results
    except (publish.PublishError, bc.BankError) as e:
        _set_report(node, "ERROR: {}".format(e))
        if hou.isUIAvailable():
            hou.ui.displayMessage(str(e), severity=hou.severityType.Error, title="Asset Bank")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _reload_bank_assets(node):
    for n in node.inputAncestors():
        if n.type().name() == BANK_ASSET:
            n.hdaModule().refresh_status(n)
            ref = n.node("reference")
            if ref is not None:
                ref.parm("reload").pressButton()
