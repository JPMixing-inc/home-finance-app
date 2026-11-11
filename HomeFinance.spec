# PyInstaller recipe for the desktop app (version comes from app/version.py). Build with build-mac.sh or build-windows.bat
# (each must run on its own system), not directly.
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

VERSION = re.search(r'VERSION = "(.+?)"', open("app/version.py").read()).group(1)
MAC = sys.platform == "darwin"
ICON = "packaging/icon.icns" if MAC else "packaging/icon.ico"

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=[("app/templates", "app/templates"), ("app/static", "app/static"), ("app/logos", "app/logos")],
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("app"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

if not MAC:  # Windows: one self-contained "Home Finance.exe" (it unpacks itself to a temp folder each launch)
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Home Finance", console=False, icon=ICON, upx=False)
else:  # Mac: the usual folder layout, wrapped in an .app bundle
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Home Finance", console=False, icon=ICON, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Home Finance", upx=False)
    app = BUNDLE(
        coll,
        name="Home Finance.app",
        icon=ICON,
        bundle_identifier="app.homefinance.desktop",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Home Finance",
            "CFBundleShortVersionString": VERSION,
            "LSApplicationCategoryType": "public.app-category.finance",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
