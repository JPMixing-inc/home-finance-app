"""Running on someone's own computer (the desktop app or run.py) instead of the NAS.

The server always listens on the network so phones can connect, and Settings has a switch
that turns phone access off: then only this computer (loopback) gets through.
"""
from __future__ import annotations

import ipaddress
import os
import socket

ENABLED = os.environ.get("HOME_FINANCE_LOCAL") == "1"
SETTING = "share_on_network"
_sharing: bool | None = None  # cached; the middleware checks it on every request


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def setting(key: str, default: str = "") -> str:
    from . import db
    conn = db.connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else default


def save_setting(key: str, value: str) -> None:
    from . import db
    conn = db.connect()
    try:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        conn.commit()
    finally:
        conn.close()


def sharing_on() -> bool:
    global _sharing
    if _sharing is None:
        _sharing = setting(SETTING, "1") == "1"  # on unless turned off
    return _sharing


def set_sharing(on: bool) -> None:
    global _sharing
    save_setting(SETTING, "1" if on else "0")
    _sharing = on


def lan_address() -> str | None:
    """This computer's address on the home network (no traffic is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def qr_svg(text: str) -> str:
    """Inline SVG QR code, or "" if segno isn't installed."""
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
