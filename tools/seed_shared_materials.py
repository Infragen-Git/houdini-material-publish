"""Publishes each material of an existing materials.usda (the hand-written one with 12 UsdPreviewSurface
materials) as v001 of its code, so the bank starts from today's colours.

    hython tools/seed_shared_materials.py [materials.usda] [--dry-run]

Codes come from the file's rules (rel material:binding:collection:GV = [..., </Asset/mtl/Galvanized>]).
Uses the bank URL and token of the Asset Bank settings.
"""
import os
import re
import sys
import tempfile

from pxr import Sdf

from asset_bank import core
from asset_bank_publish import publish

src = next((a for a in sys.argv[1:] if not a.startswith("--")),
           os.path.join(core.library(), "_materials", "materials.usda"))
dry = "--dry-run" in sys.argv
layer = Sdf.Layer.FindOrOpen(src)
rules = {}  # material name -> code
for prop in layer.GetPrimAtPath("/Asset/geo").properties:
    m = re.match(r"material:binding:collection:([A-Z]{2,3})$", prop.name)
    if m:
        targets = list(prop.targetPathList.GetAddedOrExplicitItems())
        rules[targets[-1].name] = m.group(1)
print("{} codes in {}".format(len(rules), src))
for name, code in sorted(rules.items(), key=lambda kv: kv[1]):
    out = tempfile.mkdtemp(prefix="seed-{}-".format(code))
    rep = publish.prepare_code(src, "/Asset/mtl/" + name, code, name, out)
    print(rep.text())
    if not dry:
        res = publish.upload(core.url(), "api/materials/" + code, out,
                             {"name": name, "user": os.environ.get("USERNAME", ""),
                              "description": "seed from the hand-written materials.usda", "source": src},
                             core.token())
        print("   ->", res["code"], res["published"], "created" if res["created"] else "unchanged")
