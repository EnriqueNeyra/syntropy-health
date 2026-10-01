# PyInstaller build for the Mac and Windows apps:  pyinstaller desktop/syntropy-health.spec
# Mac: dist/Syntropy Health.app. Windows: dist/Syntropy Health/ with "Syntropy Health.exe" (the app) and
# "syntropy-health.exe" (the same program with a console, for the command line and AI apps' stdio MCP).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from app.core.config import APP_VERSION  # noqa: E402
DESKTOP = ROOT / "desktop"

datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "app" / "core" / "migrations"), "app/core/migrations"),
    (str(ROOT / "app" / "data"), "app/data"),
    (str(DESKTOP / "icons"), "icons"),
]
hidden = [*collect_submodules("app"), *collect_submodules("uvicorn"), *collect_submodules("zeroconf"), "ifaddr", "tray", "updater"]
if sys.platform == "win32":
    hidden += ["pystray._win32", "webview.platforms.edgechromium", "webview.platforms.winforms"]
elif sys.platform == "darwin":
    hidden += ["webview.platforms.cocoa", "mac_window"]

a = Analysis([str(DESKTOP / "syntropy_desktop.py")], pathex=[str(ROOT), str(DESKTOP)], datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "pytest", "IPython"], noarchive=False)
pyz = PYZ(a.pure)

app_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Syntropy Health", console=False,
              icon=str(DESKTOP / "icons" / ("icon.ico" if sys.platform == "win32" else "icon.icns")),
              codesign_identity=None, entitlements_file=None)
programs = [app_exe]
if sys.platform == "win32":
    programs.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="syntropy-health", console=True,
                        icon=str(DESKTOP / "icons" / "icon.ico")))

coll = COLLECT(*programs, a.binaries, a.datas, name="Syntropy Health")

if sys.platform == "darwin":
    BUNDLE(coll, name="Syntropy Health.app", icon=str(DESKTOP / "icons" / "icon.icns"),
           bundle_identifier="io.syntropyhealth.desktop", version=APP_VERSION,
           info_plist={
               "CFBundleName": "Syntropy Health", "CFBundleDisplayName": "Syntropy Health",
               "CFBundleShortVersionString": APP_VERSION, "CFBundleVersion": APP_VERSION,
               "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
               "LSApplicationCategoryType": "public.app-category.healthcare-fitness",
               "NSHumanReadableCopyright": "© 2026 Syntropy Labs",
               # The window loads the app from its own server on this computer.
               "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
               "NSLocalNetworkUsageDescription": "Syntropy Health lets your iPhone and other devices on your network "
                                                 "reach it when you turn that on.",
               # Announced as syntropyhealth.local when other devices may connect.
               "NSBonjourServices": ["_http._tcp"],
           })
