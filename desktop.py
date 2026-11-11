"""Home Finance as a desktop app: its own window on this computer, plus the web server so
phones on the same Wi-Fi can use it while the app is open. Packaged by HomeFinance.spec.
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
import threading
import time
import webbrowser

import run
import webview
from webview.menu import Menu, MenuAction, MenuSeparator

TITLE = "Home Finance"


def send_output_to_log(data_dir: str) -> None:
    """A windowed app has no terminal (on Windows, sys.stdout is None, which breaks uvicorn)."""
    if sys.stdout is None or getattr(sys, "frozen", False):
        log = open(os.path.join(data_dir, "home-finance.log"), "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log


def show_folder(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: S606 - opening our own data folder in Explorer
    else:
        subprocess.Popen(["xdg-open", path])


def error_window(message: str, data_dir: str) -> None:
    body = (f"<body style='font-family:system-ui;background:#050506;color:#f7f7f9;padding:24px'>"
            f"<h2>Home Finance couldn't start</h2><p>{html.escape(message)}</p>"
            f"<p style='color:#92929c'>Details are in home-finance.log in {html.escape(data_dir)}</p>")
    webview.create_window(TITLE, html=body, width=560, height=300, background_color="#050506")
    webview.start()


def main() -> None:
    data_dir = run.default_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    send_output_to_log(data_dir)
    config = run.load_config(data_dir)
    webview.settings["ALLOW_DOWNLOADS"] = True  # backups and CSV exports

    try:
        port, running = run.choose_port(int(config["port"]))
    except RuntimeError as e:
        return error_window(str(e), data_dir)

    server = thread = None
    if not running:  # otherwise another copy is already serving; just open a window onto it
        try:
            server = run.make_server(data_dir, port)
        except Exception as e:  # noqa: BLE001 - show anything that stops the app loading
            import traceback
            traceback.print_exc()
            return error_window(f"{type(e).__name__}: {e}", data_dir)
        thread = threading.Thread(target=server.run, daemon=True, name="web-server")
        thread.start()
        deadline = time.monotonic() + 60
        while not run.is_home_finance(port):
            if not thread.is_alive() or time.monotonic() > deadline:
                return error_window(f"The web server didn't start on port {port}.", data_dir)
            time.sleep(0.2)

    home = f"http://127.0.0.1:{port}/"
    window = webview.create_window(TITLE, home, width=1280, height=860, min_size=(420, 500),
                                   background_color="#050506", text_select=True, zoomable=True)
    from app import updater
    updater.on_restart = window.destroy  # the updater closes the app, then swaps files and reopens it

    def current_page() -> str:
        url = window.get_current_url() or home
        return url.replace("127.0.0.1", "localhost", 1)

    menu = [Menu("Tools", [
        MenuAction("Open in Web Browser", lambda: webbrowser.open(current_page())),
        MenuAction("Phone Access…", lambda: window.load_url(home + "settings#phone")),
        MenuAction("Check for Updates…", lambda: window.load_url(home + "update")),
        MenuSeparator(),
        MenuAction("Show Data Folder", lambda: show_folder(data_dir)),
    ])]

    def offer_update() -> None:
        """At startup: if GitHub has a newer release, ask whether to install it now."""
        from app import local
        from app.version import VERSION
        if local.setting(updater.SETTING, "1") != "1":
            return
        if not updater.wait_for_check(20) or not updater.available():
            return
        message = (f"Home Finance {updater.state['latest']} is available. You have {VERSION}.\n\n"
                   "Update now? It takes a minute, and your data isn't touched.")
        if window.create_confirmation_dialog("Update available", message):
            window.load_url(home + "update?start=1")

    webview.start(offer_update if server else None, menu=menu, private_mode=False,
                  storage_path=os.path.join(data_dir, "window"))

    if server:  # window closed: stop serving phones too
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
