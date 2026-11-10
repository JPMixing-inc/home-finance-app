"""Storing transactions and auto-categorizing them. Shared by file imports and bank sync."""
from __future__ import annotations

import re
from typing import Optional

from . import importers

PAYMENT_WORDS = re.compile(r"PAYMENT|PYMT|PMT|AUTOPAY|AUTO-PAY", re.I)
NOT_A_PAYMENT = re.compile(r"CASH ?BACK|REWARD|BONUS|REFUND|RETURN|DISPUTE|ADJUSTMENT", re.I)
# Categories a payment match may overwrite: nothing a person picked on purpose for spending.
REPLACEABLE = """(category_id IS NULL OR category_id IN (SELECT id FROM categories
    WHERE kind IN ('transfer', 'income') OR name IN ('Debt Payments', 'Miscellaneous')))"""


def load_rules(conn) -> list[tuple[str, int]]:
    rows = conn.execute("SELECT pattern, category_id FROM rules ORDER BY length(pattern) DESC").fetchall()
    return [(r["pattern"].lower(), r["category_id"]) for r in rows]


def categorize(conn, rules, desc: str, hint: Optional[str] = None) -> Optional[int]:
    """Rules first, then whatever this exact description was last time, then the bank's guess."""
    lowered = desc.lower()
    for pattern, cid in rules:
        if pattern in lowered:
            return cid
    row = conn.execute(
        "SELECT category_id FROM transactions WHERE description = ? AND category_id IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (desc,),
    ).fetchone()
    if row:
        return row[0]
    if hint:
        row = conn.execute("SELECT id FROM categories WHERE name = ?", (hint,)).fetchone()
        return row[0] if row else None
    return None


def store(conn, rules, account_id: int, tx: dict, fingerprint: str, import_id: int,
          match_existing: bool = False) -> bool:
    """Insert one transaction. Returns False if we already had it.

    With match_existing, a row we already have from a file import or manual entry (same account
    and amount, date within 3 days) is claimed by this fingerprint instead of duplicated.
    """
    if match_existing:
        if conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND fingerprint = ?",
                        (account_id, fingerprint)).fetchone():
            return False
        twin = conn.execute(
            """SELECT id FROM transactions
               WHERE account_id = ? AND amount_cents = ?
                 AND (fingerprint IS NULL OR fingerprint NOT LIKE 'plaid:%')
                 AND date BETWEEN date(?, '-3 days') AND date(?, '+3 days')
               ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1""",
            (account_id, tx["amount_cents"], tx["date"], tx["date"], tx["date"]),
        ).fetchone()
        if twin:
            conn.execute("UPDATE transactions SET fingerprint = ? WHERE id = ?", (fingerprint, twin["id"]))
            return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO transactions
           (account_id, date, description, amount_cents, category_id, import_id, fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (account_id, tx["date"], tx["description"], tx["amount_cents"],
         categorize(conn, rules, tx["description"], tx.get("hint")), import_id, fingerprint),
    )
    return cur.rowcount > 0


def import_file(conn, account_id: int, txs: list[dict], filename: str, user_id: int) -> int:
    cur = conn.execute("INSERT INTO imports (account_id, filename, user_id) VALUES (?, ?, ?)",
                       (account_id, filename, user_id))
    import_id = cur.lastrowid
    rules = load_rules(conn)
    added = skipped = 0
    for tx, fp in zip(txs, importers.fingerprints(txs)):
        if store(conn, rules, account_id, tx, fp, import_id):
            added += 1
        else:
            skipped += 1
    conn.execute("UPDATE imports SET added = ?, skipped = ? WHERE id = ?", (added, skipped, import_id))
    conn.commit()
    mark_debt_payments(conn)
    return import_id


def apply_rules_to_uncategorized(conn) -> int:
    rules = load_rules(conn)
    changed = 0
    for tx in conn.execute("SELECT id, description FROM transactions WHERE category_id IS NULL").fetchall():
        lowered = tx["description"].lower()
        for pattern, cid in rules:
            if pattern in lowered:
                conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (cid, tx["id"]))
                changed += 1
                break
    conn.commit()
    return changed


def _category(conn, name: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def mark_debt_payments(conn) -> int:
    """Categorize payments to credit cards and loans as payments. Returns how many changed.

    1. Money leaving checking/savings that lands on a card or loan (same amount, within a few days)
       is linked as a pair and both sides become Credit Card Payment.
    2. Money arriving on a card that reads like a payment becomes Credit Card Payment.
    3. Outflows whose description contains a debt's payment_match ("To Loan 12") become Debt
       Payments (loans, hand-entered debts) or Credit Card Payment (cards).
    """
    card_pay, debt_pay = _category(conn, "Credit Card Payment"), _category(conn, "Debt Payments")
    debt_kinds = "('credit', 'loan')"
    changed = 0
    if card_pay:
        inflows = conn.execute(
            f"""SELECT t.id, t.date, t.amount_cents FROM transactions t JOIN accounts a ON a.id = t.account_id
                WHERE a.kind IN {debt_kinds} AND t.amount_cents > 0 AND t.pair_id IS NULL AND {REPLACEABLE}
                ORDER BY t.date""").fetchall()
        for t in inflows:
            m = conn.execute(
                f"""SELECT t.id FROM transactions t JOIN accounts a ON a.id = t.account_id
                    WHERE a.kind NOT IN {debt_kinds} AND t.amount_cents = ? AND t.pair_id IS NULL AND {REPLACEABLE}
                      AND t.date BETWEEN date(?, '-6 days') AND date(?, '+2 days')
                    ORDER BY abs(julianday(t.date) - julianday(?)) LIMIT 1""",
                (-t["amount_cents"], t["date"], t["date"], t["date"])).fetchone()
            if m:
                conn.execute("UPDATE transactions SET pair_id = ? WHERE id = ?", (m["id"], t["id"]))
                conn.execute("UPDATE transactions SET pair_id = ? WHERE id = ?", (t["id"], m["id"]))
                changed += conn.execute(
                    f"UPDATE transactions SET category_id = ? WHERE id IN (?, ?) AND category_id IS NOT ? AND {REPLACEABLE}",
                    (card_pay, t["id"], m["id"], card_pay)).rowcount
        for t in conn.execute(
                f"""SELECT t.id, t.description FROM transactions t JOIN accounts a ON a.id = t.account_id
                    WHERE a.kind = 'credit' AND t.amount_cents > 0 AND t.category_id IS NOT ? AND {REPLACEABLE}""",
                (card_pay,)).fetchall():
            if PAYMENT_WORDS.search(t["description"]) and not NOT_A_PAYMENT.search(t["description"]):
                conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (card_pay, t["id"]))
                changed += 1
    for d in conn.execute(
            """SELECT d.payment_match, a.kind FROM debts d LEFT JOIN accounts a ON a.id = d.account_id
               WHERE d.payment_match != ''""").fetchall():
        target = card_pay if d["kind"] == "credit" else debt_pay
        if target:
            changed += conn.execute(
                f"""UPDATE transactions SET category_id = ?
                    WHERE amount_cents < 0 AND description LIKE ? AND category_id IS NOT ? AND {REPLACEABLE}
                      AND account_id IN (SELECT id FROM accounts WHERE kind NOT IN {debt_kinds})""",
                (target, f"%{d['payment_match']}%", target)).rowcount
    conn.commit()
    return changed
