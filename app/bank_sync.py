"""Bank sync through Plaid (https://plaid.com/docs/api/products/transactions/).

Plaid keys live in data/plaid.json rather than the database, so a downloaded database backup
can't be used to pull bank data on its own.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from . import brands, db, ledger, snowball

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
SYNC_HOURS = tuple(range(0, 24, 2))  # every 2 hours, on the hour
LOGIN_ERRORS = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT", "ACCESS_NOT_GRANTED"}

log = logging.getLogger("uvicorn.error")
_lock = threading.Lock()

# Plaid's personal_finance_category -> our default category names. Most specific prefix wins.
CATEGORY_HINTS = [
    ("INCOME_WAGES", "Paycheck"),
    ("INCOME", "Other Income"),
    ("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "Credit Card Payment"),
    ("LOAN_PAYMENTS_MORTGAGE_PAYMENT", "Mortgage / Rent"),
    ("LOAN_PAYMENTS_CAR_PAYMENT", "Car Payment"),
    ("LOAN_PAYMENTS", "Debt Payments"),
    ("TRANSFER_IN", "Transfer"),
    ("TRANSFER_OUT", "Transfer"),
    ("FOOD_AND_DRINK_GROCERIES", "Groceries"),
    ("FOOD_AND_DRINK", "Restaurants"),
    ("TRANSPORTATION_GAS", "Gas"),
    ("RENT_AND_UTILITIES_RENT", "Mortgage / Rent"),
    ("RENT_AND_UTILITIES_TELEPHONE", "Internet & Phone"),
    ("RENT_AND_UTILITIES_INTERNET_AND_CABLE", "Internet & Phone"),
    ("RENT_AND_UTILITIES", "Utilities"),
    ("HOME_IMPROVEMENT", "Home Maintenance"),
    ("GENERAL_MERCHANDISE", "Shopping"),
    ("MEDICAL", "Health & Medical"),
    ("PERSONAL_CARE", "Personal Care"),
    ("ENTERTAINMENT", "Entertainment"),
    ("GENERAL_SERVICES_INSURANCE", "Insurance"),
    ("GENERAL_SERVICES_AUTOMOTIVE", "Car Maintenance"),
    ("TRAVEL", "Travel"),
    ("GOVERNMENT_AND_NON_PROFIT_DONATIONS", "Gifts & Donations"),
]


class PlaidError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- config

def config_path() -> str:
    return os.path.join(db.DATA_DIR, "plaid.json")


def load_config() -> Optional[dict]:
    try:
        with open(config_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_config(client_id: str, secret: str, env: str) -> None:
    path = config_path()
    with open(path, "w") as f:
        json.dump({"client_id": client_id, "secret": secret, "env": env}, f)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- API

def call(path: str, payload: dict, config: Optional[dict] = None) -> dict:
    config = config or load_config()
    if not config:
        raise PlaidError("NOT_CONFIGURED", "Plaid keys aren't set up yet.")
    body = dict(payload, client_id=config["client_id"], secret=config["secret"])
    req = urllib.request.Request(HOSTS[config["env"]] + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except ValueError:
            raise PlaidError(f"HTTP_{e.code}", f"Plaid returned HTTP {e.code}") from None
        raise PlaidError(err.get("error_code", "ERROR"),
                         err.get("display_message") or err.get("error_message") or "Plaid error") from None
    except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, dropped connections
        raise PlaidError("NETWORK", f"Couldn't reach Plaid: {getattr(e, 'reason', e)}") from None


def link_token(access_token: Optional[str] = None, config: Optional[dict] = None) -> str:
    """A token for Plaid Link. With an access_token, Link opens in update mode to fix a login."""
    payload = {"client_name": "Home Finance", "language": "en", "country_codes": ["US"],
               "user": {"client_user_id": "household"}}
    if access_token:
        payload["access_token"] = access_token
    else:
        payload.update(products=["transactions"], transactions={"days_requested": 730})
    return call("/link/token/create", payload, config)["link_token"]


# ---------------------------------------------------------------- helpers

def to_cents(amount) -> int:
    """Plaid amounts are positive when money leaves the account; ours are the opposite."""
    return -int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def category_hint(tx: dict) -> Optional[str]:
    pfc = (tx.get("personal_finance_category") or {}).get("detailed", "")
    for prefix, name in CATEGORY_HINTS:
        if pfc.startswith(prefix):
            return name
    return None


def account_kind(acct: dict) -> str:
    if acct.get("type") in ("credit", "loan"):
        return acct["type"]
    if acct.get("type") == "investment":
        return "investment"
    return "savings" if acct.get("subtype") in ("savings", "money market", "cd") else "checking"


def update_accounts(conn, item_row_id: int, accounts: list[dict]) -> None:
    """Record any new Plaid accounts (unmapped) and refresh balances on mapped ones."""
    now = datetime.now().isoformat(timespec="minutes")
    for a in accounts:
        conn.execute(
            """INSERT INTO plaid_accounts (item_id, plaid_account_id, name, mask, type, subtype)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (plaid_account_id) DO UPDATE SET
               name = excluded.name, mask = excluded.mask, type = excluded.type, subtype = excluded.subtype""",
            (item_row_id, a["account_id"], a.get("name") or "", a.get("mask") or "",
             a.get("type") or "", a.get("subtype") or ""),
        )
        balances = a.get("balances") or {}
        current = balances.get("current")
        if current is None:
            current = balances.get("available")
        if current is None:
            continue
        cents = int((Decimal(str(current)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if a.get("type") in ("credit", "loan"):
            cents = -cents  # what's owed
        conn.execute(
            """UPDATE accounts SET balance_cents = ?, balance_at = ? WHERE id =
               (SELECT account_id FROM plaid_accounts WHERE plaid_account_id = ?)""",
            (cents, now, a["account_id"]),
        )


# ---------------------------------------------------------------- linking

def refresh_institution(conn, item_row_id: int, access_token: str, config: dict) -> None:
    """Store the bank's Plaid institution id, brand color and logo (shown beside its transactions)."""
    try:
        inst_id = call("/item/get", {"access_token": access_token}, config)["item"]["institution_id"]
        inst = call("/institutions/get_by_id", {"institution_id": inst_id, "country_codes": ["US"],
                                                "options": {"include_optional_metadata": True}}, config)["institution"]
    except (PlaidError, KeyError) as e:
        log.warning("Couldn't load bank details for item %s: %s", item_row_id, e)
        return
    color = brands.COLOR_OVERRIDES.get(inst_id) or brands.valid_color(inst.get("primary_color") or "")
    conn.execute(
        """UPDATE plaid_items SET institution_id = ?, logo = ?,
                  color = CASE WHEN color = '' THEN ? ELSE color END WHERE id = ?""",
        (inst_id, inst.get("logo") or brands.bundled_logo(inst_id), color, item_row_id))
    conn.commit()


def add_item(public_token: str, institution: str) -> int:
    config = load_config()
    res = call("/item/public_token/exchange", {"public_token": public_token}, config)
    access_token, item_id = res["access_token"], res["item_id"]
    accounts = call("/accounts/get", {"access_token": access_token}, config)["accounts"]
    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO plaid_items (item_id, access_token, env, institution) VALUES (?, ?, ?, ?)
               ON CONFLICT (item_id) DO UPDATE SET access_token = excluded.access_token""",
            (item_id, access_token, config["env"], institution or "Bank"),
        )
        row_id = conn.execute("SELECT id FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()[0]
        update_accounts(conn, row_id, accounts)
        conn.commit()
        refresh_institution(conn, row_id, access_token, config)
        return row_id
    finally:
        conn.close()


def remove_item(item_row_id: int) -> None:
    conn = db.connect()
    try:
        item = conn.execute("SELECT * FROM plaid_items WHERE id = ?", (item_row_id,)).fetchone()
        if not item:
            return
        config = load_config()
        if config and config["env"] == item["env"]:
            try:
                call("/item/remove", {"access_token": item["access_token"]}, config)
            except PlaidError as e:
                log.warning("Plaid item/remove failed: %s", e)
        conn.execute("DELETE FROM plaid_items WHERE id = ?", (item_row_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- syncing

def _fetch_changes(access_token: str, cursor: str, config: dict) -> tuple[list, list, list, list, str]:
    for _ in range(3):
        added, modified, removed, accounts, cur = [], [], [], [], cursor
        try:
            while True:
                payload = {"access_token": access_token, "count": 500}
                if cur:
                    payload["cursor"] = cur
                res = call("/transactions/sync", payload, config)
                added += res["added"]
                modified += res["modified"]
                removed += res["removed"]
                accounts = res.get("accounts") or accounts
                cur = res["next_cursor"]
                if not res["has_more"]:
                    return added, modified, removed, accounts, cur
        except PlaidError as e:
            if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise
    raise PlaidError("SYNC_RETRY", "The bank's data kept changing during sync. Try again in a minute.")


def sync_item(item_row_id: int) -> dict:
    """Pull new, changed and removed transactions for one bank connection."""
    with _lock:
        conn = db.connect()
        try:
            item = conn.execute("SELECT * FROM plaid_items WHERE id = ?", (item_row_id,)).fetchone()
            config = load_config()
            if not item or not config or item["env"] != config["env"]:
                raise PlaidError("ENV_MISMATCH", "This connection was made with different Plaid keys.")
            try:
                added, modified, removed, accounts, cursor = _fetch_changes(
                    item["access_token"], item["cursor"], config)
            except PlaidError as e:
                status = "login_required" if e.code in LOGIN_ERRORS else "error"
                conn.execute("UPDATE plaid_items SET status = ?, error = ? WHERE id = ?",
                             (status, str(e), item_row_id))
                conn.commit()
                raise

            if not item["institution_id"]:
                refresh_institution(conn, item_row_id, item["access_token"], config)
            # The sync response only lists accounts that had activity; /accounts/get (free, cached
            # balances) has every account.
            try:
                accounts = call("/accounts/get", {"access_token": item["access_token"]}, config)["accounts"]
            except PlaidError as e:
                log.warning("Plaid accounts/get failed for %s: %s", item["institution"], e)
            update_accounts(conn, item_row_id, accounts)
            # Loans sync their balance only: their side of a payment would cancel out the payment
            # already counted from checking.
            mapping = {r["plaid_account_id"]: r["account_id"] for r in conn.execute(
                """SELECT p.plaid_account_id, p.account_id FROM plaid_accounts p JOIN accounts a ON a.id = p.account_id
                   WHERE p.item_id = ? AND a.kind != 'loan'""", (item_row_id,))}
            rules = ledger.load_rules(conn)
            imports: dict[int, list] = {}  # local account id -> [import id, added, skipped]
            for tx in added:
                account_id = mapping.get(tx["account_id"])
                if tx.get("pending") or not account_id:
                    continue  # pending ones come back as new transactions once they post
                if account_id not in imports:
                    cur = conn.execute(
                        "INSERT INTO imports (account_id, filename, source) VALUES (?, ?, 'sync')",
                        (account_id, f"Bank sync: {item['institution']}"))
                    imports[account_id] = [cur.lastrowid, 0, 0]
                entry = imports[account_id]
                record = {"date": tx["date"], "description": tx.get("merchant_name") or tx.get("name") or "Unknown",
                          "amount_cents": to_cents(tx["amount"]), "hint": category_hint(tx)}
                if ledger.store(conn, rules, account_id, record, "plaid:" + tx["transaction_id"], entry[0],
                                match_existing=True):
                    entry[1] += 1
                else:
                    entry[2] += 1
            for tx in modified:
                conn.execute(
                    "UPDATE transactions SET date = ?, amount_cents = ? WHERE fingerprint = ?",
                    (tx["date"], to_cents(tx["amount"]), "plaid:" + tx["transaction_id"]))
            for tx in removed:
                conn.execute("DELETE FROM transactions WHERE fingerprint = ?", ("plaid:" + tx["transaction_id"],))
            ledger.mark_debt_payments(conn)
            for import_id, n_added, n_skipped in imports.values():
                if n_added:
                    conn.execute("UPDATE imports SET added = ?, skipped = ? WHERE id = ?",
                                 (n_added, n_skipped, import_id))
                else:
                    conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
            conn.execute(
                "UPDATE plaid_items SET cursor = ?, status = 'ok', error = '', last_sync = ? WHERE id = ?",
                (cursor, datetime.now().isoformat(timespec="minutes"), item_row_id))
            conn.commit()
            snowball.record_balances(conn)
            return {"added": sum(e[1] for e in imports.values()), "removed": len(removed),
                    "first": item["last_sync"] is None}
        finally:
            conn.close()


def sync_all() -> dict:
    """Sync every connection that's ready. Returns totals plus a list of failures."""
    config = load_config()
    if not config:
        return {"added": 0, "errors": []}
    conn = db.connect()
    try:
        items = conn.execute(
            "SELECT id, institution FROM plaid_items WHERE status IN ('ok', 'error') AND env = ?",
            (config["env"],)).fetchall()
    finally:
        conn.close()
    total, errors = 0, []
    for item in items:
        try:
            total += sync_item(item["id"])["added"]
        except PlaidError as e:
            errors.append(f"{item['institution']}: {e}")
            log.warning("Bank sync failed for %s: %s", item["institution"], e)
    return {"added": total, "errors": errors}


def start_scheduler() -> None:
    def loop():
        while True:
            now = datetime.now()
            today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in SYNC_HOURS]
            upcoming = [t for t in today if t > now] or [today[0] + timedelta(days=1)]
            time.sleep((upcoming[0] - now).total_seconds())
            try:
                result = sync_all()
                log.info("Scheduled bank sync: %s new transactions", result["added"])
            except Exception:
                log.exception("Scheduled bank sync crashed")

    threading.Thread(target=loop, daemon=True, name="bank-sync").start()
