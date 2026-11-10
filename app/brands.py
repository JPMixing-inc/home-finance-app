"""Bank brand colors and logos, shown beside each account's transactions.

Colors come from Plaid's institution metadata unless someone picks one on the Accounts or Bank
sync page. Each color gets a dark-theme and a light-theme variant nudged just far enough toward
white or black to stay visible (3:1) against that theme's surface.
"""
from __future__ import annotations

import base64
import os
import re

from markupsafe import Markup, escape

# Plaid's listed color for these doesn't match the logo people recognize.
COLOR_OVERRIDES = {
    "ins_128026": "#d03027",  # Capital One: the red swoosh (Plaid lists a generic blue)
}
# Plaid's institution names, shortened to what's on the card
SHORT_NAMES = {
    "Citibank Online": "Citi",
    "Community First Credit Union of Florida": "Community First",
    "VyStar Credit Union": "VyStar",
}
DARK_SURFACE, LIGHT_SURFACE = (18, 18, 20), (255, 255, 255)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _luminance(rgb) -> float:
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _visible_on(rgb, surface, toward) -> str:
    for step in range(21):
        t = step / 20
        mixed = tuple(round(c + (w - c) * t) for c, w in zip(rgb, toward))
        if _contrast(mixed, surface) >= 3:
            break
    return "#%02x%02x%02x" % mixed


def variants(hex_color: str) -> tuple[str, str]:
    rgb = _rgb(hex_color)
    return _visible_on(rgb, DARK_SURFACE, (255, 255, 255)), _visible_on(rgb, LIGHT_SURFACE, (0, 0, 0))


def valid_color(value: str) -> str:
    value = (value or "").strip()
    return value.lower() if HEX.match(value) else ""


LOGO_DIR = os.path.join(os.path.dirname(__file__), "logos")


def bundled_logo(institution_id: str) -> str:
    """Base64 PNG from app/logos/<institution id>.png, for banks Plaid has no logo for (Chase, Citi)."""
    path = os.path.join(LOGO_DIR, f"{institution_id}.png")
    if not institution_id or not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def fill_logos(conn) -> None:
    for item_id, inst_id in conn.execute(
            "SELECT id, institution_id FROM plaid_items WHERE logo = '' AND institution_id != ''").fetchall():
        logo = bundled_logo(inst_id)
        if logo:
            conn.execute("UPDATE plaid_items SET logo = ? WHERE id = ?", (logo, item_id))
    conn.commit()


def short_name(institution: str) -> str:
    institution = (institution or "").strip()
    return SHORT_NAMES.get(institution) or re.sub(r"\s+Online$", "", institution)


def account_brands(conn) -> dict[int, dict]:
    """account id -> {color, d, l, logo (plaid_items id with a logo, or None), bank}"""
    rows = conn.execute(
        """SELECT a.id, a.color AS own,
                  (SELECT pi.color FROM plaid_accounts p JOIN plaid_items pi ON pi.id = p.item_id
                    WHERE p.account_id = a.id LIMIT 1) AS bank_color,
                  (SELECT pi.institution FROM plaid_accounts p JOIN plaid_items pi ON pi.id = p.item_id
                    WHERE p.account_id = a.id LIMIT 1) AS bank,
                  (SELECT pi.id FROM plaid_accounts p JOIN plaid_items pi ON pi.id = p.item_id
                    WHERE p.account_id = a.id AND pi.logo != '' LIMIT 1) AS logo_item
           FROM accounts a""").fetchall()
    out = {}
    for r in rows:
        color = valid_color(r["own"] or "") or valid_color(r["bank_color"] or "")
        if color:
            dark, light = variants(color)
            out[r["id"]] = {"color": color, "d": dark, "l": light, "logo": r["logo_item"], "bank": r["bank"],
                            "own": valid_color(r["own"] or ""), "bank_color": valid_color(r["bank_color"] or "")}
    return out


def bank_attrs(brand) -> Markup:
    """Attributes that give an element its bank's color (see [data-bank] in style.css)."""
    if not brand:
        return Markup("")
    return Markup('data-bank style="--bank-d: %s; --bank-l: %s"') % (brand["d"], brand["l"])


def bank_mark(brand) -> Markup:
    """The bank's small logo, or a dot in its color."""
    if not brand:
        return Markup("")
    if brand.get("logo"):
        return Markup('<img class="bank-logo" src="/bank-logo/%s" alt="%s" title="%s">') % (
            brand["logo"], brand.get("bank") or "", brand.get("bank") or "")
    return Markup('<span class="bank-dot" title="%s"></span>') % escape(brand.get("bank") or "")
