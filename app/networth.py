"""Net worth: what the household owns (cash, savings, investments) minus what it owes.

Account balances come from bank sync. Hand-entered debts (e.g. student loans imported from a
statement) count as owed whether or not they're in the snowball plan.
"""
from __future__ import annotations

from datetime import date

GROUPS = [  # (key, label, account kinds)
    ("cash", "Cash", ("checking", "cash")),
    ("savings", "Savings", ("savings",)),
    ("investments", "Investments", ("investment",)),
    ("debt", "Debt", ("credit", "loan")),
]


def summary(conn) -> dict:
    groups = {key: {"label": label, "total": 0, "items": []} for key, label, _ in GROUPS}
    kind_to_group = {kind: key for key, _, kinds in GROUPS for kind in kinds}
    missing = []
    for a in conn.execute("SELECT id, name, kind, balance_cents, balance_at FROM accounts ORDER BY name"):
        key = kind_to_group.get(a["kind"], "cash")
        if a["balance_cents"] is None:
            missing.append(a["name"])
            continue
        groups[key]["items"].append({"id": a["id"], "name": a["name"], "balance": a["balance_cents"]})
        groups[key]["total"] += a["balance_cents"]
    for d in conn.execute(
            """SELECT name, balance_cents, group_name FROM debts
               WHERE account_id IS NULL AND balance_cents IS NOT NULL AND payoff_status = ''"""):
        groups["debt"]["items"].append({"id": None, "name": d["name"], "balance": -d["balance_cents"],
                                        "group": d["group_name"]})
        groups["debt"]["total"] -= d["balance_cents"]
    assets = groups["cash"]["total"] + groups["savings"]["total"] + groups["investments"]["total"]
    owed = -groups["debt"]["total"]
    for g in groups.values():
        g["items"].sort(key=lambda i: -abs(i["balance"]))
    return {"groups": groups, "assets": assets, "owed": owed, "net": assets - owed, "missing": missing,
            "change": _change(conn, assets - owed)}


def record(conn, s: dict) -> None:
    """One snapshot per day, so the dashboard can show the change over time."""
    conn.execute(
        """INSERT INTO net_worth_history (date, assets_cents, debts_cents) VALUES (?, ?, ?)
           ON CONFLICT (date) DO UPDATE SET assets_cents = excluded.assets_cents, debts_cents = excluded.debts_cents""",
        (date.today().isoformat(), s["assets"], s["owed"]))
    conn.commit()


def _change(conn, net_now: int):
    """Change vs. the oldest snapshot from the last 30 days (or the oldest one at all)."""
    row = conn.execute(
        """SELECT date, assets_cents - debts_cents AS net FROM net_worth_history
           WHERE date < ? ORDER BY (date >= date('now', '-30 days')) DESC, date ASC LIMIT 1""",
        (date.today().isoformat(),)).fetchone()
    if not row:
        return None
    return {"since": row["date"], "amount": net_now - row["net"]}
