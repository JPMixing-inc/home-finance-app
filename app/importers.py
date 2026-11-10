"""Parse bank exports (CSV, OFX/QFX) into plain transaction dicts.

A parsed transaction is {"date": "YYYY-MM-DD", "description": str, "amount_cents": int,
"fitid": str | None}. Spending is negative, money in is positive.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import re
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d",
    "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d",
]


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_date(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    candidates = [value, value.split("T")[0], value.split(" ")[0]]
    for text in candidates:
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def parse_amount(value: str) -> int | None:
    """'$1,234.56' -> 123456, '(12.00)' -> -1200, '-5' -> -500."""
    text = value.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("+"):
        text = text[1:]
    try:
        cents = int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return -cents if negative else cents


# ---------------------------------------------------------------- CSV

def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """Return (header, data rows), skipping any summary lines banks put above the header."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header_at = 0
    for i, row in enumerate(rows[:25]):
        cells = [c.strip().lower() for c in row]
        if len(cells) >= 3 and any("date" in c for c in cells):
            header_at = i
            break
    header = [c.strip() for c in rows[header_at]]
    width = len(header)
    data = [(r + [""] * width)[:width] for r in rows[header_at + 1:]]
    return header, data


def _find(header: list[str], *needles: str, exclude: tuple[str, ...] = ()) -> int | None:
    lowered = [h.lower() for h in header]
    for needle in needles:
        for i, h in enumerate(lowered):
            if needle in h and not any(x in h for x in exclude):
                return i
    return None


def guess_mapping(header: list[str], rows: list[list[str]], account_kind: str) -> dict:
    lowered = [h.lower() for h in header]
    date_col = next((i for i, h in enumerate(lowered) if h in ("date", "transaction date", "trans. date")), None)
    if date_col is None:
        date_col = _find(header, "date")
    desc_col = _find(header, "description", "payee", "merchant", "name", "details", "memo")
    amount_col = _find(header, "amount", exclude=("balance",))
    debit_col = _find(header, "debit", "withdrawal")
    credit_col = _find(header, "credit", "deposit", exclude=("card",))
    if amount_col is not None:
        debit_col = credit_col = None

    flip = False
    if amount_col is not None and account_kind == "credit":
        values = [parse_amount(r[amount_col]) for r in rows[:200]]
        values = [v for v in values if v]
        # Card exports usually list purchases as positive numbers
        flip = bool(values) and sum(v > 0 for v in values) > len(values) * 0.6
    return {
        "date": date_col, "description": desc_col, "amount": amount_col,
        "debit": debit_col, "credit": credit_col, "flip": flip,
    }


def apply_mapping(rows: list[list[str]], mapping: dict) -> tuple[list[dict], int]:
    """Turn CSV rows into transactions. Returns (transactions, unreadable row count)."""
    out, bad = [], 0
    for row in rows:
        try:
            date = parse_date(row[mapping["date"]])
            desc = " ".join(row[mapping["description"]].split())
            if mapping.get("amount") is not None:
                amount = parse_amount(row[mapping["amount"]])
            else:
                debit = parse_amount(row[mapping["debit"]]) if mapping.get("debit") is not None else None
                credit = parse_amount(row[mapping["credit"]]) if mapping.get("credit") is not None else None
                if debit is None and credit is None:
                    amount = None
                else:
                    amount = abs(credit or 0) - abs(debit or 0)
        except (IndexError, TypeError):
            bad += 1
            continue
        if date is None or amount is None or not desc:
            bad += 1
            continue
        if mapping.get("flip"):
            amount = -amount
        out.append({"date": date, "description": desc, "amount_cents": amount, "fitid": None})
    return out, bad


# ---------------------------------------------------------------- OFX / QFX

def is_ofx(text: str) -> bool:
    head = text[:2000].upper()
    return "OFXHEADER" in head or "<OFX>" in head


def parse_ofx(text: str) -> list[dict]:
    """Tolerant of both SGML (no closing tags) and XML flavours of OFX."""
    out = []
    blocks = re.split(r"<STMTTRN>", text, flags=re.I)[1:]
    for block in blocks:
        block = re.split(r"</STMTTRN>|</BANKTRANLIST>", block, flags=re.I)[0]

        def tag(name: str) -> str:
            m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
            return html.unescape(m.group(1)).strip() if m else ""

        posted = tag("DTPOSTED")
        date = parse_date(posted[:8]) if len(posted) >= 8 else None
        amount = parse_amount(tag("TRNAMT"))
        name, memo = tag("NAME"), tag("MEMO")
        desc = name or memo
        if name and memo and memo.lower() not in name.lower() and len(name) < 32:
            desc = f"{name} {memo}"
        if date is None or amount is None or not desc:
            continue
        out.append({"date": date, "description": " ".join(desc.split()),
                    "amount_cents": amount, "fitid": tag("FITID") or None})
    return out


# ---------------------------------------------------------------- de-duplication

def normalize(desc: str) -> str:
    return re.sub(r"[^a-z0-9]", "", desc.lower())


def fingerprints(transactions: list[dict]) -> list[str]:
    """Stable IDs so re-importing an overlapping file skips rows we already have.

    Identical rows in one file (two $5 coffees the same day) get an occurrence number,
    so they stay distinct but still match the same rows in a later overlapping export.
    """
    seen: Counter = Counter()
    out = []
    for tx in transactions:
        if tx.get("fitid"):
            out.append("fitid:" + tx["fitid"])
            continue
        key = (tx["date"], tx["amount_cents"], normalize(tx["description"]))
        seen[key] += 1
        raw = f"{key[0]}|{key[1]}|{key[2]}|{seen[key]}"
        out.append(hashlib.sha1(raw.encode()).hexdigest())
    return out


def rule_suggestion(desc: str) -> str:
    """A likely 'contains' pattern for a merchant: up to two leading words, stopping at
    store numbers or reference codes ('KROGER #1234 COLUMBUS' -> 'KROGER')."""
    # Card-reader prefixes ('SQ *', 'TST*', 'PAYPAL *') say nothing about the merchant, so keep them
    # as part of the pattern and pick words from what follows.
    m = re.match(r"(SQ|TST|SP|PAYPAL|PP|DD|BT|PY)\s*\*\s*", desc, re.I)
    prefix = m.group() if m else ""
    words = []
    for token in desc[len(prefix):].split():
        word = re.split(r"[*#]", token)[0]
        if not (re.fullmatch(r"[\w&'.\-]+", word) and re.search(r"[A-Za-z]{2,}", word)):
            break
        words.append(word)
        if word != token or len(words) == 2:
            break
    return (prefix + " ".join(words)) if words else desc
