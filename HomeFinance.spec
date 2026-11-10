# PyInstaller recipe for the desktop app. Build with build-mac.sh or build-windows.bat
# (each must run on its own system), not directly.
import sys

from PyInstaller.utils.hooks import collect_submodules

VERSION = "1.0.0"
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
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Home Finance", console=False, icon=ICON, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Home Finance", upx=False)

if MAC:
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
