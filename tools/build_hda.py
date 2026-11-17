"""Builds otls/asset_bank_bank_publish.hda (asset_bank::bank_publish::1.0) from scratch.

    hython tools/build_hda.py

A shell around asset_bank_publish.publish_lop: parameters, a pass-through and an inner USD ROP that saves
what comes after the Layer Break. Needs the asset_bank package (houdini-asset-bank) installed too.
"""
import os
import shutil

import hou

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = hou.scriptLanguage.Python
ALWAYS = "{ asset == \"__never__\" } { asset != \"__never__\" }"  # disabled, value computed


def cb(fn):
    return "kwargs['node'].hdaModule().{}(kwargs['node'])".format(fn)


TOOLS_SHELF = """<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <tool name="$HDA_DEFAULT_TOOL" label="$HDA_LABEL" icon="$HDA_ICON">
    <toolMenuContext name="viewer"><contextNetType>LOP</contextNetType></toolMenuContext>
    <toolMenuContext name="network"><contextOpType>$HDA_TABLE_AND_NAME</contextOpType></toolMenuContext>
    <toolSubmenu>Asset Bank</toolSubmenu>
    <script scriptType="python"><![CDATA[import loptoolutils
loptoolutils.genericTool(kwargs, '$HDA_NAME')]]></script>
  </tool>
</shelfDocument>
"""


PUBLISH_HDA = os.path.join(HOME, "otls", "asset_bank_bank_publish.hda").replace("\\", "/")
PUBLISH_TYPE = "asset_bank::bank_publish::1.0"


def publish_parms():
    g = hou.ParmTemplateGroup()
    mode = hou.StringParmTemplate("mode", "Publish To", 1, default_value=("asset",), menu_type=hou.menuType.Normal,
                                  menu_items=("asset", "shared"),
                                  menu_labels=("Asset Material (asset/material/vNNN)",
                                               "Shared Materials (_materials, one per code)"),
                                  help="Asset: the materials after the Layer Break become the asset's own material "
                                       "layer, stronger than the code rules. Shared: each material becomes its "
                                       "code's material (name it CODE_Name, e.g. GV_Galvanized, or use a name the "
                                       "bank already has).")
    g.append(mode)
    asset = hou.StringParmTemplate("asset", "Asset", 1, default_value=("",),
                                   default_expression=("hou.pwd().hdaModule().detect_asset(hou.pwd())",),
                                   default_expression_language=(PY,),
                                   help="Found from the Bank Asset node upstream (or the stage's defaultPrim).")
    asset.setConditional(hou.parmCondType.HideWhen, '{ mode != "asset" }')
    g.append(asset)
    g.append(hou.StringParmTemplate("description", "Comment", 1, default_value=("",)))
    g.append(hou.ButtonParmTemplate("check", "Check", script_callback=cb("check"), script_callback_language=PY,
                                    join_with_next=True, help="Prepare without sending: shows what would be published."))
    g.append(hou.ButtonParmTemplate("publish", "Publish", script_callback=cb("run"), script_callback_language=PY,
                                    join_with_next=True))
    g.append(hou.ButtonParmTemplate("openpanel", "Asset Bank Panel",
                                    script_callback="__import__('asset_bank.lop').lop.open_panel()",
                                    script_callback_language=PY))
    report = hou.StringParmTemplate("report", "Report", 1, default_value=("Put this node after your material "
                                    "work, itself after a Layer Break placed below the Bank Asset node.",))
    report.setTags({"editor": "1", "editorlines": "12-40"})
    g.append(report)
    save = hou.StringParmTemplate("savepath", "Save Path", 1, default_value=("$HOUDINI_TEMP_DIR/asset_bank_publish.usda",))
    save.hide(True)  # set by the Python before the inner USD ROP renders (inner nodes are locked)
    g.append(save)
    return g


def build_publish():
    stage = hou.node("/stage")
    sub = stage.createNode("subnet", "bank_publish")
    for child in sub.children():
        if child.type().name() != "output":
            child.destroy()
    out = [c for c in sub.children() if c.type().name() == "output"][0]
    out.setInput(0, sub.indirectInputs()[0])  # pass-through
    rop = sub.createNode("usd_rop", "save")
    rop.setInput(0, sub.indirectInputs()[0])
    rop.parm("savestyle").set("flattenalllayers")  # one file, whatever the material nodes created
    rop.parm("lopoutput").setExpression('chs("../savepath")', hou.exprLanguage.Hscript)
    sub.layoutChildren()

    if os.path.exists(PUBLISH_HDA):
        os.replace(PUBLISH_HDA, PUBLISH_HDA + ".bak")
    hda = sub.createDigitalAsset(name=PUBLISH_TYPE, hda_file_name=PUBLISH_HDA, description="Bank Publish Material",
                                 min_num_inputs=1, max_num_inputs=1, ignore_external_references=True)
    d = hda.type().definition()
    d.setParmTemplateGroup(publish_parms())
    d.addSection("PythonModule", "from asset_bank_publish.publish_lop import *  # logic in python/asset_bank_publish/publish_lop.py\n")
    d.setExtraFileOption("PythonModule/IsPython", True)
    d.addSection("Tools.shelf", TOOLS_SHELF)
    d.setIcon("ROP_usd")
    d.setComment("Publish the materials made after a Layer Break to the Asset Bank (asset or shared library).")
    d.setVersion("0.1.0")
    d.save(PUBLISH_HDA, template_node=hda)
    print("saved", PUBLISH_HDA)


if __name__ == "__main__":
    build_publish()
    shutil.rmtree(os.path.join(HOME, "otls", "backup"), ignore_errors=True)
    for f in os.listdir(os.path.join(HOME, "otls")):
        if f.endswith(".bak"):
            os.remove(os.path.join(HOME, "otls", f))
