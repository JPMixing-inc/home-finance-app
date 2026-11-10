"""Run Home Finance on this computer (Mac, Windows or Linux) in a terminal, using your web browser.

The desktop app (desktop.py) uses the helpers here too. Started by "Start Home Finance.command"
(Mac) or "Start Home Finance.bat" (Windows), or directly:

    python run.py [--port 8090] [--data FOLDER] [--no-browser]

The port and whether to open a browser can also be set in server.json in the data folder.
Phones on the same network can connect unless phone access is turned off in Settings.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"port": 8090, "open_browser": True}


def default_data_dir() -> str:
    if os.environ.get("FINANCE_DATA"):
        return os.path.abspath(os.environ["FINANCE_DATA"])
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Home Finance")


def load_config(data_dir: str) -> dict:
    path = os.path.join(data_dir, "server.json")
    os.makedirs(data_dir, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
        return dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError) as e:
        print(f"Couldn't read {path} ({e}); using the defaults.")
        return dict(DEFAULTS)


def is_home_finance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            return json.load(resp).get("app") == "home-finance"
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def choose_port(preferred: int) -> tuple[int, bool]:
    """(port, already_running). Uses the next free port if another program has the preferred one."""
    if is_home_finance(preferred):
        return preferred, True
    for port in range(preferred, preferred + 50):
        if port_free(port):
            return port, False
    raise RuntimeError(f"Port {preferred} is in use and no nearby port is free.")


def wait_until_ready(port: int, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_home_finance(port):
            return True
        time.sleep(0.25)
    return False


def make_server(data_dir: str, port: int):
    """A uvicorn server for the app, listening on the network. Call before anything imports app.*."""
    # The app reads these when it is imported.
    os.environ["FINANCE_DATA"] = data_dir
    os.environ["HOME_FINANCE_LOCAL"] = "1"
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import uvicorn
    from app.main import app

    return uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Home Finance on this computer.")
    parser.add_argument("--data", help="folder for the database and settings")
    parser.add_argument("--port", type=int, help="port to listen on (default 8090)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data) if args.data else default_data_dir()
    config = load_config(data_dir)
    wanted = args.port or int(os.environ.get("PORT") or config["port"])
    try:
        port, running = choose_port(wanted)
    except RuntimeError as e:
        sys.exit(f"{e} Pick another with --port.")
    url = f"http://localhost:{port}"
    if running:
        print(f"Home Finance is already running at {url}. Opening it.")
        webbrowser.open(url)
        return
    if port != wanted:
        print(f"Port {wanted} is in use by another program; using {port} instead.")

    server = make_server(data_dir, port)
    from app import local

    print()
    print("  Home Finance is running.")
    print(f"  Open:        {url}")
    ip = local.lan_address()
    if ip and local.sharing_on():
        print(f"  On phones:   http://{ip}:{port}   (turn this off in Settings > Phone access)")
    print(f"  Data folder: {data_dir}")
    print("  Keep this window open while you use it. Close it (or press Ctrl+C) to stop.")
    print(flush=True)

    if config["open_browser"] and not args.no_browser:
        threading.Thread(target=lambda: wait_until_ready(port) and webbrowser.open(url), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
