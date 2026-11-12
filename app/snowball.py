"""Debt snowball: pay minimums on everything, put every extra dollar on the smallest balance, and
roll each paid-off debt's payment into the next one.

debts.included: 1 = in the snowball (prioritized smallest first), 2 = counted in the totals but not
prioritized (minimums only until every prioritized debt is gone), 0 = left out entirely.

A debt is either linked to an account (credit card or loan; its balance comes from bank sync) or
entered by hand. Balances here are positive cents owed.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date
from typing import Optional

from . import brands

MAX_MONTHS = 600
NO_PAYMENT = re.compile(r"grace|forbearance|deferment|in school", re.I)
SERVICERS = ["MOHELA", "Nelnet", "Aidvantage", "EdFinancial", "Navient", "Sallie Mae", "SoFi", "Earnest",
             "Great Lakes", "FedLoan", "Discover Student Loans", "Citizens", "College Ave"]


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def sync_debts(conn) -> None:
    """Add any credit card or loan account that isn't in the plan yet."""
    today = date.today().isoformat()
    for a in conn.execute(
        """SELECT * FROM accounts a WHERE kind IN ('credit', 'loan')
           AND NOT EXISTS (SELECT 1 FROM debts d WHERE d.account_id = a.id)""").fetchall():
        owed = max(0, -(a["balance_cents"] or 0))
        conn.execute(
            "INSERT INTO debts (account_id, name, start_balance_cents, start_date) VALUES (?, ?, ?, ?)",
            (a["id"], a["name"], owed, today))
    conn.commit()


def load_debts(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT d.*, a.balance_cents AS account_balance, a.balance_at, a.kind AS account_kind,
                  (SELECT pi.institution FROM plaid_accounts p JOIN plaid_items pi ON pi.id = p.item_id
                    WHERE p.account_id = d.account_id LIMIT 1) AS bank
           FROM debts d LEFT JOIN accounts a ON a.id = d.account_id ORDER BY d.id""").fetchall()
    month_start = date.today().strftime("%Y-%m-01")
    debts = []
    for r in rows:
        d = dict(r)
        d["lender"] = brands.short_name(d["bank"]) or d["lender"] or d["group_name"]
        if d["name"].isupper():  # "CREDIT CARD ••5798" -> "Credit Card ••5798"
            d["name"] = d["name"].title()
        if d["lender"] and d["name"].lower().startswith(d["lender"].lower() + " "):
            d["name"] = d["name"][len(d["lender"]) + 1:]  # "Discover it Card", not "Discover Discover it Card"
        d["title"] = f"{d['lender']} {d['name']}" if d["lender"] else d["name"]
        if d["account_id"] and d["account_balance"] is not None:
            d["balance"] = max(0, -d["account_balance"])
            d["synced"] = True
        else:
            d["balance"] = d["balance_cents"] or 0
            d["synced"] = False
        d["info"] = json.loads(d["details"]) if d["details"] else {}
        d["known"] = d["synced"] or d["balance_cents"] is not None
        # Marked paid off by hand: counts as $0 here whatever the bank (or a stale balance) says.
        d["closed"] = d["payoff_status"] == "closed"
        d["bank_balance"] = None
        if d["payoff_status"]:
            if d["synced"] and not d["closed"] and d["balance"] > 0:
                d["bank_balance"] = d["balance"]  # bank still shows something owed
            d["balance"], d["known"] = 0, True
        start = d["start_balance_cents"] or 0
        if not start and d["known"]:
            # Added before its first balance arrived: count progress from the first known balance
            conn.execute("UPDATE debts SET start_balance_cents = ? WHERE id = ?", (d["balance"], d["id"]))
            start = d["balance"]
        d["no_payment_due"] = not d["min_payment_cents"] and bool(NO_PAYMENT.search(d["info"].get("status", "")))
        if d["min_payment_cents"] or d["no_payment_due"]:
            d["min"], d["min_estimated"] = d["min_payment_cents"], False
        else:
            # Typical card minimum: 1% of the balance plus the month's interest, at least $25
            interest = d["balance"] * (d["apr"] or 0) / 1200
            d["min"], d["min_estimated"] = min(d["balance"], max(2500, round(d["balance"] / 100 + interest))), True
        d["start"] = max(start, d["balance"])
        d["paid"] = d["start"] - d["balance"]
        d["pct"] = round(d["paid"] * 100 / d["start"]) if d["start"] else (100 if d["balance"] == 0 else 0)
        d["paid_off"] = d["known"] and d["balance"] == 0
        d["payments"] = payments(conn, d)
        d["paid_this_month"] = sum(p["amount"] for p in d["payments"] if p["date"] >= month_start)
        d["last_payment"] = d["payments"][0] if d["payments"] else None
        debts.append(d)
    conn.commit()
    return debts


def payments(conn, d: dict, limit: int = 12) -> list[dict]:
    """Recent payments to a debt, newest first: what arrived on the card, or (for loans and
    hand-entered debts) what left checking with the debt's payment_match in the description."""
    if d["account_id"] and d.get("account_kind") == "credit":
        rows = conn.execute(
            """SELECT date, amount_cents FROM transactions WHERE account_id = ? AND amount_cents > 0
               AND category_id = (SELECT id FROM categories WHERE name = 'Credit Card Payment')
               ORDER BY date DESC LIMIT ?""", (d["account_id"], limit)).fetchall()
        return [{"date": r[0], "amount": r[1]} for r in rows]
    if d.get("payment_match"):
        rows = conn.execute(
            """SELECT date, -amount_cents FROM transactions WHERE amount_cents < 0 AND description LIKE ?
               AND account_id IN (SELECT id FROM accounts WHERE kind NOT IN ('credit', 'loan'))
               ORDER BY date DESC LIMIT ?""", (f"%{d['payment_match']}%", limit)).fetchall()
        return [{"date": r[0], "amount": r[1]} for r in rows]
    return []


def snowball_order(debts: list[dict]) -> list[dict]:
    """Smallest balance first; a tie goes to the higher interest rate."""
    return sorted(debts, key=lambda d: (d["balance"], -(d["apr"] or 0)))


def simulate(debts: list[dict], extra: int, roll_over: bool = True, presorted: bool = False) -> dict:
    """Month-by-month payoff. With roll_over=False it's minimum payments only (no extra, and a
    paid-off debt's minimum isn't moved to the next one) for comparison."""
    open_debts = [d for d in debts if d["balance"] > 0]
    order = open_debts if presorted else snowball_order(open_debts)
    bal = {d["id"]: float(d["balance"]) for d in order}
    budget = sum(d["min"] for d in order) + (extra if roll_over else 0)
    payoff, interest, month = {}, 0.0, 0
    totals = [sum(bal.values())]  # total owed at the start, then after each month
    while any(b > 0.5 for b in bal.values()) and month < MAX_MONTHS:
        month += 1
        for d in order:
            if bal[d["id"]] > 0:
                i = bal[d["id"]] * (d["apr"] or 0) / 1200
                bal[d["id"]] += i
                interest += i
        available = budget if roll_over else sum(d["min"] for d in order if bal[d["id"]] > 0)
        for d in order:  # minimums first
            if bal[d["id"]] > 0:
                pay = min(d["min"], bal[d["id"]], available)
                bal[d["id"]] -= pay
                available -= pay
        for d in order:  # everything left goes to the smallest remaining balance
            if available <= 0:
                break
            if bal[d["id"]] > 0:
                pay = min(available, bal[d["id"]])
                bal[d["id"]] -= pay
                available -= pay
        for d in order:
            if bal[d["id"]] <= 0.5 and d["id"] not in payoff:
                payoff[d["id"]] = month
        totals.append(sum(max(0.0, b) for b in bal.values()))
    done = all(b <= 0.5 for b in bal.values())
    return {"months": month if done else None, "payoff": payoff, "interest": round(interest),
            "monthly": budget, "totals": [round(t) for t in totals]}


def month_after(n: int) -> date:
    """The month n-1 months from now (month 1 is this month)."""
    today = date.today()
    total = today.year * 12 + today.month - 1 + (n - 1)
    return date(total // 12, total % 12 + 1, 1)


def plan(conn) -> dict:
    sync_debts(conn)
    everything = load_debts(conn)
    debts = [d for d in everything if d["included"] in (1, 2)]
    open_debts = [d for d in debts if d["known"] and not d["paid_off"]]
    priority = snowball_order([d for d in open_debts if d["included"] == 1])
    side = snowball_order([d for d in open_debts if d["included"] == 2])
    active = priority + side  # counted-but-not-prioritized debts get the snowball last
    # The monthly amount is entered either as extra on top of the minimums, or as the total that goes
    # to debt each month (then the extra is whatever's left after this month's minimums).
    mode = get_setting(conn, "snowball_mode", "extra")
    min_total = sum(d["min"] for d in active)
    if mode == "total":
        amount = int(get_setting(conn, "snowball_total_cents", "0") or 0)
        extra, short = max(0, amount - min_total), max(0, min_total - amount)
    else:
        amount = extra = int(get_setting(conn, "snowball_extra_cents", "0") or 0)
        short = 0
    snow = simulate(active, extra, presorted=True)
    base = simulate(active, 0, roll_over=False, presorted=True)
    for d in active:
        m = snow["payoff"].get(d["id"])
        d["payoff_date"] = month_after(m) if m else None
    total = sum(d["balance"] for d in active)
    chart = path_chart(active, snow, base)
    start_total = sum(d["start"] for d in debts if d["known"])
    paid_total = sum(d["paid"] for d in debts if d["known"])
    paid_off = [d for d in debts if d["paid_off"]]
    cleared_total = sum(d["start"] for d in paid_off)  # debts gone completely (green on the bar)
    return {
        "debts": priority,
        "side": side,
        "active": active,
        "paid_off": sorted(paid_off, key=lambda d: d.get("paid_off_on") or "", reverse=True),
        "cleared_total": cleared_total,
        "pct_cleared": round(cleared_total * 100 / start_total, 1) if start_total else 0,
        "pct_progress": round((paid_total - cleared_total) * 100 / start_total, 1) if start_total else 0,
        "excluded": [d for d in everything if not d["included"]],
        "groups": groups(everything),
        "target": active[0] if active else None,
        "next": active[1] if len(active) > 1 else None,
        "extra": extra,
        "mode": mode,
        "amount": amount,
        "min_total": min_total,
        "short": short,
        "total": total,
        "start_total": start_total,
        "paid_total": paid_total,
        "pct": round(paid_total * 100 / start_total) if start_total else 0,
        "snow": snow,
        "base": base,
        "debt_free": month_after(snow["months"]) if snow["months"] else None,
        "months_saved": (base["months"] - snow["months"]) if base["months"] and snow["months"] else None,
        "interest_saved": base["interest"] - snow["interest"] if base["months"] and snow["months"] else None,
        "unknown": [d for d in debts if not d["known"]],
        "estimated": any(d["min_estimated"] for d in active),
        "chart": chart,
        "timeline": [{"name": d["name"], "lender": d["lender"], "m": snow["payoff"].get(d["id"]), "target": i == 0,
                      "balance": d["balance"], "side": d["included"] == 2,
                      "date": d["payoff_date"].strftime("%b %Y") if d.get("payoff_date") else None}
                     for i, d in enumerate(active)],
        "no_apr": any(not d["apr"] for d in active),
    }


def path_chart(active: list[dict], snow: dict, base: dict) -> dict:
    """Month-by-month total owed on the snowball plan vs minimums only, for the chart."""
    n = len(snow["totals"])
    horizon = min(MAX_MONTHS + 1, max(n * 2, n + 12, 13))
    if snow["months"] is None:
        horizon = min(horizon, 121)
    # Point m is the balance after m months of payments, so it lines up with month_after(m),
    # the same date the payoff dates use. Point 0 is today.
    labels = [month_after(max(1, m)).strftime("%b %Y") for m in range(horizon)]
    names = {d["id"]: d["title"] for d in active}
    events = sorted(({"m": m, "name": names[i]} for i, m in snow["payoff"].items()), key=lambda e: e["m"])
    return {"labels": labels, "snow": snow["totals"][:horizon], "base": base["totals"][:horizon],
            "events": events, "base_months": base["months"], "snow_months": snow["months"]}


def record_balances(conn) -> None:
    """One balance snapshot per debt per day, for tracking progress over time."""
    today = date.today().isoformat()
    for d in load_debts(conn):
        if d["known"]:
            conn.execute(
                """INSERT INTO debt_history (debt_id, date, balance_cents) VALUES (?, ?, ?)
                   ON CONFLICT (debt_id, date) DO UPDATE SET balance_cents = excluded.balance_cents""",
                (d["id"], today, d["balance"]))
    conn.commit()


def first_paid_off_date(conn, debt_id: int) -> Optional[str]:
    row = conn.execute("SELECT paid_off_on FROM debts WHERE id = ?", (debt_id,)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute("SELECT MIN(date) FROM debt_history WHERE debt_id = ? AND balance_cents = 0",
                       (debt_id,)).fetchone()
    return row[0] if row else None


def mark_paid_off(conn, debt_id: int, closed: bool) -> Optional[str]:
    """Mark a debt paid off by hand (closed=True: the account is closed too). Returns its title.

    The original balance is kept as the starting balance so the paid-off list can show it. A closed
    account's balance is set to $0 and bank sync stops changing it (see bank_sync.update_accounts),
    so net worth and the accounts page agree.
    """
    d = next((x for x in load_debts(conn) if x["id"] == debt_id), None)
    if not d:
        return None
    conn.execute(  # d["start"] is already the larger of the starting and current balance
        """UPDATE debts SET payoff_status = ?, paid_off_on = COALESCE(paid_off_on, ?),
                  start_balance_cents = ? WHERE id = ?""",
        ("closed" if closed else "paid", date.today().isoformat(), d["start"], debt_id))
    if closed and d["account_id"]:
        conn.execute("UPDATE accounts SET balance_cents = 0, balance_at = ? WHERE id = ?",
                     (date.today().isoformat(), d["account_id"]))
    conn.commit()
    record_balances(conn)
    return d["title"]


def reopen(conn, debt_id: int) -> None:
    """Undo a by-hand payoff. A synced account gets its real balance back at the next bank sync."""
    # The $0 snapshots taken while it was marked paid off weren't real balances
    conn.execute("""DELETE FROM debt_history WHERE debt_id = ? AND balance_cents = 0
                    AND date >= (SELECT paid_off_on FROM debts WHERE id = ?)""", (debt_id, debt_id))
    conn.execute("UPDATE debts SET payoff_status = '', paid_off_on = NULL WHERE id = ?", (debt_id,))
    conn.commit()


# ---------------------------------------------------------------- loan statements

LOAN_LABELS = {"school": "school", "due date": "due_date", "loan status": "status",
               "estimated payoff date": "payoff_date", "repayment plan": "plan", "loan type": "type"}
MONEY_RATE = re.compile(r"^\$\s?(?P<balance>[\d,]+\.\d{2})\s+(?P<rate>\d{1,2}(?:\.\d+)?)\s*%$")
ONE_LINE = re.compile(r"^(?P<name>.*[A-Za-z].*?)\s+\$\s?(?P<balance>[\d,]+\.\d{2})\s+(?P<rate>\d{1,2}(?:\.\d+)?)\s*%$")
LOAN_HEADER = re.compile(r"^\d+-\d+\s+[A-Za-z].*(loan|direct|stafford|perkins|plus|consolidat)", re.I)


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)


def parse_loan_statement(text: str) -> dict:
    """Read loans from a servicer's "all loan details" page (PDF text or pasted).

    Handles both the compact table ("1-01 Direct Loan - Unsubsidized $6,099.10 4.990%") and the
    expanded view, where each loan's name line is followed by label/value lines (School, Due Date,
    Loan Status, ...) and then a "$balance rate%" line.
    """
    lines = [" ".join(l.split()) for l in text.splitlines()]
    lines = [l for l in lines if l]
    as_of = re.search(r"As of (\d{1,2}/\d{1,2}/\d{4})", text)
    servicer = next((s for s in SERVICERS if s.lower() in text.lower()), "")
    loans, i = [], 0
    while i < len(lines):
        line = lines[i]
        one = ONE_LINE.match(line)
        if one and not re.search(r"total", one["name"], re.I):
            loans.append(_loan(one["name"], one["balance"], one["rate"], {}))
            i += 1
            continue
        if LOAN_HEADER.match(line):
            info, j = {}, i + 1
            while j < len(lines) and j - i <= 20 and not LOAN_HEADER.match(lines[j]):
                money = MONEY_RATE.match(lines[j])
                if money:
                    loans.append(_loan(line, money["balance"], money["rate"], info))
                    break
                key = LOAN_LABELS.get(lines[j].lower())
                if key and j + 1 < len(lines):
                    value = lines[j + 1]
                    if value.lower() not in LOAN_LABELS and not MONEY_RATE.match(value) and not LOAN_HEADER.match(value):
                        info[key] = value
                        j += 1
                j += 1
            i = j if j > i else i + 1
            continue
        i += 1
    return {"as_of": as_of.group(1) if as_of else "", "servicer": servicer, "loans": loans}


def _loan(name: str, balance: str, rate: str, info: dict) -> dict:
    return {"name": name.strip(" -:"), "balance": int(round(float(balance.replace(",", "")) * 100)),
            "apr": float(rate), "info": info}


def consolidated_rate(debts: list[dict]) -> float:
    """Federal Direct Consolidation: balance-weighted average rate, rounded up to the next 1/8%."""
    total = sum(d["balance"] for d in debts)
    if not total:
        return 0.0
    weighted = sum(d["balance"] * (d["apr"] or 0) for d in debts) / total
    eighths = -(-round(weighted * 8, 6) // 1)  # ceil, ignoring float noise
    return round(eighths / 8, 3)


def groups(debts: list[dict]) -> list[dict]:
    """Hand-entered debts imported together (e.g. all MOHELA loans), for consolidating or removing."""
    out = {}
    for d in debts:
        if d["group_name"] and not d["account_id"]:
            out.setdefault(d["group_name"], []).append(d)
    result = []
    for name, items in out.items():
        result.append({"name": name, "debts": items, "balance": sum(d["balance"] for d in items),
                       "rate": consolidated_rate([d for d in items if d["known"]]),
                       "min": sum(d["min_payment_cents"] for d in items)})
    return result
