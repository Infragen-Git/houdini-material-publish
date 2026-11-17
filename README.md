# Houdini Material Publish

Publishes materials made in Houdini (MaterialX) to the InfraGen Asset Bank. Needs the
[houdini-asset-bank](https://github.com/Infragen-Git/houdini-asset-bank) package (bank client, settings,
Bank Asset LOP).

## Workflow
```
Bank Asset (the asset, from the bank)
  └ Layer Break
      └ Material Library / Assign Material ... (your look-dev)
          └ Bank Publish Material   -> Check, then Publish
```
The **Bank Publish Material** LOP (`asset_bank::bank_publish::1.0`, Tab > Asset Bank) saves only what
comes after the Layer Break, prepares it and sends it with the publish token (Asset Bank panel > Settings).
After a publish the local library is updated and the Bank Asset nodes upstream reload.

### Asset Material
The materials become the asset's own layer, `<Asset>/material/vNNN` (the server numbers versions; an
identical re-publish creates nothing). They win over the shared code rules, which keep covering the pieces
you did not assign. On the way:
- every material moves to `/<Asset>/looks/` (outside the asset it would be lost when InfraGen references
  it; `/<Asset>/mtl` is where the shared materials compose);
- bindings are pushed down to each mesh / GeomSubset: a binding on `geo` would lose against the code
  rules, which sit on `geo` too;
- a UsdPreviewSurface is added when Houdini made none (OpenPBR), from the surface's constant values, so
  InfraGen, Blender, usdview and the bank's web page see the colour;
- textures are copied next to the layer and made relative (`./textures/...`, `<UDIM>` tiles included).
  One publish must stay under 100 MB.

### Shared Materials
Each material becomes its code's shared material, `_materials/<CODE>/vNNN`; the server rewrites
`_materials/materials.usda` (one rule per code, magenta `Missing` for a `*_geo` mesh with no known code).
Name a material `CODE_Name` (`GV_Galvanized`, 2–3 capitals) or reuse a name the bank already has
(`Concrete` -> CN). A new code is created on its first publish.

## Install
Installed and updated together with houdini-asset-bank by its `install.ps1` (see that README):
```
powershell -ExecutionPolicy Bypass -File "$([Environment]::GetFolderPath('MyDocuments'))\houdini22.0sset_bank\install.ps1"
```
Then Asset Bank panel > Settings > *Publish token* (the same token as the Blender exporter's).

## Layout
```
packages/asset_bank_publish.json      copy into <houdini prefs>/packages
python/asset_bank_publish/
    publish.py                        prepares the layer (pure pxr: looks/, bindings, previews, textures), uploads
    publish_lop.py                    the publish HDA's logic
otls/asset_bank_bank_publish.hda      built by tools/build_hda.py
tools/seed_shared_materials.py        publishes a hand-written materials.usda as v001 of each code
```
`hython tools/build_hda.py` rebuilds the HDA (from Git Bash: `env -u HOME hython ...`).
