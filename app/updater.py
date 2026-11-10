"""Check GitHub for a newer release and install it in place (desktop app only).

Flow: check() asks the GitHub API for the latest release. start() downloads the right asset
(.dmg on Mac, .zip on Windows), checks its SHA-256 against GitHub's digest, unpacks it next to
the app, and starts a small helper script that waits for this process to exit, swaps the new
files in and relaunches. The data folder is never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from typing import Callable, Optional

from .version import VERSION

REPO = "JPMixing-inc/home-finance-app"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
SETTING = "update_check"  # "0" turns off the check at startup

FROZEN = bool(getattr(sys, "frozen", False))
MAC, WINDOWS = sys.platform == "darwin", os.name == "nt"

# Set by desktop.py: closes the window so the app exits and the helper can swap files.
on_restart: Optional[Callable[[], None]] = None

_lock = threading.Lock()
_checked = threading.Event()
state: dict = {
    "latest": None,     # "1.2.0"
    "notes": "",        # release notes (markdown)
    "page": RELEASES_PAGE,
    "asset": None,      # {"name", "url", "size", "digest"}
    "error": "",
    "phase": "idle",    # idle, downloading, installing, restarting, failed
    "progress": 0.0,    # 0..1 while downloading
}


def parse(version: str) -> tuple:
    parts = []
    for piece in version.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def available() -> bool:
    return bool(state["latest"]) and parse(state["latest"]) > parse(VERSION)


def asset_name() -> Optional[str]:
    if MAC:
        return "HomeFinance-Mac-AppleSilicon.dmg" if platform.machine() == "arm64" else "HomeFinance-Mac-Intel.dmg"
    if WINDOWS:
        return "HomeFinance-Windows.zip"
    return None


def _get(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"HomeFinance/{VERSION}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check() -> dict:
    """Ask GitHub for the latest release. Safe to call from any thread."""
    try:
        with _get(API) as resp:
            release = json.load(resp)
        wanted = asset_name()
        asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
        with _lock:
            state.update(latest=release.get("tag_name", "").lstrip("vV") or None, notes=release.get("body") or "",
                         page=release.get("html_url") or RELEASES_PAGE, error="",
                         asset={"name": asset["name"], "url": asset["browser_download_url"],
                                "size": asset.get("size") or 0, "digest": asset.get("digest") or ""}
                         if asset else None)
    except Exception as e:  # noqa: BLE001 - offline, rate-limited, GitHub down: just report it
        with _lock:
            state["error"] = f"Couldn't check for updates: {e}"
    _checked.set()
    return state


def check_in_background() -> None:
    threading.Thread(target=check, daemon=True, name="update-check").start()


def wait_for_check(timeout: float) -> bool:
    return _checked.wait(timeout)


# ---------------------------------------------------------------- installing

def install_target() -> Optional[str]:
    """The folder or .app bundle to replace, when this is a packaged app."""
    if not FROZEN:
        return None
    exe = os.path.realpath(sys.executable)
    if MAC:  # .../Home Finance.app/Contents/MacOS/Home Finance
        bundle = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        return bundle if bundle.endswith(".app") else None
    if WINDOWS:
        return os.path.dirname(exe)
    return None


def _writable(folder: str) -> bool:
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def why_not_automatic() -> str:
    """Empty if the app can update itself, otherwise the reason."""
    target = install_target()
    if not FROZEN:
        return "You're running Home Finance from source. Update with git pull, or download the app."
    if not target:
        return "Automatic updates aren't supported on this system."
    if not state["asset"]:
        return "This release has no download for your computer."
    parent = os.path.dirname(target)
    target_ok = os.access(target, os.W_OK) if MAC else _writable(target)  # don't write into a signed .app
    if "AppTranslocation" in target or not _writable(parent) or not target_ok:
        return ("Home Finance can't replace itself where it's installed. On a Mac, move it to Applications "
                "first. Otherwise download the new version and replace the old one.")
    return ""


def start() -> str:
    """Begin downloading and installing. Returns an error message, or "" once started."""
    with _lock:
        if state["phase"] in ("downloading", "installing", "restarting"):
            return ""
        if not available():
            return "There's no newer version to install."
        reason = why_not_automatic()
        if reason:
            return reason
        state.update(phase="downloading", progress=0.0, error="")
    threading.Thread(target=_install, daemon=True, name="update-install").start()
    return ""


def _fail(message: str) -> None:
    with _lock:
        state.update(phase="failed", error=message)


def _download(asset: dict, dest: str) -> None:
    digest = hashlib.sha256()
    total = asset["size"] or 0
    done = 0
    with _get(asset["url"], timeout=60) as resp, open(dest, "wb") as out:
        total = total or int(resp.headers.get("Content-Length") or 0)
        while chunk := resp.read(256 * 1024):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                state["progress"] = min(done / total, 1.0)
    expected = asset["digest"]
    if expected.startswith("sha256:") and expected[7:] != digest.hexdigest():
        raise RuntimeError("The download didn't match GitHub's checksum, so it wasn't installed.")


def _install() -> None:
    target = install_target()
    stage = tempfile.mkdtemp(prefix="home-finance-update-")
    try:
        package = os.path.join(stage, state["asset"]["name"])
        _download(state["asset"], package)
        state.update(phase="installing", progress=1.0)
        helper = _prepare_mac(package, stage, target) if MAC else _prepare_windows(package, stage, target)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(stage, ignore_errors=True)
        return _fail(str(e) or type(e).__name__)

    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    log = open(os.path.join(os.environ.get("FINANCE_DATA") or stage, "update.log"), "wb")
    if MAC:
        subprocess.Popen(helper, env=env, stdout=log, stderr=log, start_new_session=True)
    else:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(helper, env=env, stdout=log, stderr=log, creationflags=flags)
    state["phase"] = "restarting"
    if on_restart:
        threading.Timer(1.5, on_restart).start()  # give the page a moment to show "Restarting…"


def _prepare_mac(dmg: str, stage: str, target: str) -> list[str]:
    mount = os.path.join(stage, "mnt")
    os.makedirs(mount)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-quiet",
                    "-mountpoint", mount, dmg], check=True, capture_output=True)
    try:
        app = next((n for n in os.listdir(mount) if n.endswith(".app")), None)
        if not app:
            raise RuntimeError("The download doesn't contain the app.")
        new_app = os.path.join(stage, "Home Finance.app")
        subprocess.run(["ditto", os.path.join(mount, app), new_app], check=True, capture_output=True)
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", mount], capture_output=True)
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", new_app], capture_output=True)

    script = os.path.join(stage, "update.sh")
    with open(script, "w") as f:
        f.write(MAC_HELPER)
    reopen_env = f"FINANCE_DATA={os.environ['FINANCE_DATA']}" if os.environ.get("FINANCE_DATA") else ""
    return ["/bin/sh", script, str(os.getpid()), new_app, target, stage, reopen_env]


MAC_HELPER = r"""#!/bin/sh
# Home Finance updater: wait for the app to quit, swap in the new bundle, reopen it.
PID="$1"; NEW="$2"; TARGET="$3"; STAGE="$4"; REOPEN_ENV="$5"
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done
rm -rf "$TARGET.old"
if mv "$TARGET" "$TARGET.old" && mv "$NEW" "$TARGET"; then
  rm -rf "$TARGET.old"
else
  echo "Swap failed; putting the old version back."
  [ -d "$TARGET.old" ] && { rm -rf "$TARGET"; mv "$TARGET.old" "$TARGET"; }
fi
if [ -n "$REOPEN_ENV" ]; then open --env "$REOPEN_ENV" "$TARGET"; else open "$TARGET"; fi
rm -rf "$STAGE"
"""


def _prepare_windows(package: str, stage: str, target: str) -> list[str]:
    unpacked = os.path.join(stage, "new")
    with zipfile.ZipFile(package) as z:
        z.extractall(unpacked)
    new = os.path.join(unpacked, "Home Finance")
    if not os.path.isfile(os.path.join(new, "Home Finance.exe")):
        raise RuntimeError("The download doesn't contain Home Finance.exe.")
    script = os.path.join(stage, "update.ps1")
    with open(script, "w", encoding="utf-8") as f:
        f.write(WINDOWS_HELPER)
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script,
            "-ProcId", str(os.getpid()), "-New", new, "-Dir", target, "-Stage", stage]


WINDOWS_HELPER = r"""# Home Finance updater: wait for the app to quit, copy the new files over, reopen it.
param([int]$ProcId, [string]$New, [string]$Dir, [string]$Stage)
Wait-Process -Id $ProcId -Timeout 120 -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
robocopy "$New\_internal" "$Dir\_internal" /MIR /R:10 /W:1 /NFL /NDL /NJH /NJS | Out-Null
foreach ($i in 1..20) {
  try { Copy-Item "$New\Home Finance.exe" "$Dir\Home Finance.exe" -Force -ErrorAction Stop; break }
  catch { Start-Sleep -Seconds 1 }
}
Start-Process -FilePath "$Dir\Home Finance.exe" -WorkingDirectory $Dir
Start-Sleep -Seconds 2
Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
"""
