"""Home Finance: a small household budget site for the home network."""
from __future__ import annotations

import base64
import csv
import html
import io
import itertools
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional
from urllib.parse import urlencode

import markdown as md
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware

from . import ai, auth, bank_sync, brands, db, importers, ledger, local, networth, snowball, updater
from .version import VERSION

BASE = os.path.dirname(__file__)
MAX_UPLOAD = 20 * 1024 * 1024
ACCOUNT_KINDS = ["checking", "savings", "investment", "credit", "loan", "cash"]

db.init()
_conn = db.connect()
ledger.mark_debt_payments(_conn)  # catch up on payments from before this existed
_conn.close()


@asynccontextmanager
async def lifespan(_app):
    conn = db.connect()
    try:
        brands.fill_logos(conn)
    finally:
        conn.close()
    bank_sync.start_scheduler()
    if local.ENABLED and local.setting(updater.SETTING, "1") == "1":
        updater.check_in_background()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# ---------------------------------------------------------------- formatting helpers

def money(cents: Optional[int], signed: bool = False) -> str:
    cents = cents or 0
    text = f"${abs(cents) / 100:,.2f}"
    if cents < 0:
        return "−" + text
    return ("+" + text) if signed and cents > 0 else text


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def valid_month(value: Optional[str]) -> str:
    return value if value and re.fullmatch(r"\d{4}-\d{2}", value) else current_month()


def shift_month(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_label(month: str) -> str:
    y, m = map(int, month.split("-"))
    return date(y, m, 1).strftime("%B %Y")


def month_range(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{shift_month(month, 1)}-01"


templates.env.filters["money"] = money
templates.env.filters["month_label"] = month_label
templates.env.globals["bank_attrs"] = brands.bank_attrs
templates.env.globals["bank_mark"] = brands.bank_mark


def render(request: Request, name: str, **ctx) -> Response:
    ctx["me"] = request.session.get("username")
    ctx["ai_ready"] = ai.load_key() is not None
    ctx["path"] = request.url.path
    ctx["local_mode"] = local.ENABLED
    ctx["version"] = VERSION
    ctx["update_banner"] = (local.ENABLED and updater.available() and on_this_computer(request)
                            and request.session.get("update_dismissed") != updater.state["latest"]
                            and request.url.path != "/update")
    ctx["update_latest"] = updater.state["latest"]
    return templates.TemplateResponse(request, name, ctx)


def on_this_computer(request: Request) -> bool:
    return local.is_loopback(request.client.host if request.client else None)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- auth

PUBLIC = ("/login", "/setup", "/static/", "/healthz")


PHONES_OFF = """<!doctype html><meta name="viewport" content="width=device-width">
<title>Home Finance</title><body style="font-family:system-ui;background:#050506;color:#f7f7f9;padding:2em">
<h1>Phone access is off</h1><p>Turn it on in Home Finance on the computer: Settings, then Phone access.</p>"""


@app.middleware("http")
async def require_login(request: Request, call_next):
    if (local.ENABLED and request.url.path != "/healthz" and not local.sharing_on()
            and not local.is_loopback(request.client.host if request.client else None)):
        return HTMLResponse(PHONES_OFF, status_code=403)
    if request.url.path.startswith(("/update", "/api/update")):
        # Updating is for the desktop app's own computer only, and works before anyone has logged in.
        if not (local.ENABLED and on_this_computer(request)):
            return HTMLResponse("Not found", status_code=404)
        return await call_next(request)
    if not request.url.path.startswith(PUBLIC) and not request.session.get("uid"):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "signed out"}, status_code=401)
        return back("/setup" if db.user_count() == 0 else "/login")
    return await call_next(request)


# Added after the auth middleware so it wraps it (sessions are loaded first).
app.add_middleware(SessionMiddleware, secret_key=auth.secret_key(),
                   session_cookie="finance_session", max_age=60 * 60 * 24 * 30, same_site="lax")


def get_conn() -> sqlite3.Connection:
    return db.connect()


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "home-finance", "version": VERSION}  # run.py looks for "app" to spot a running copy


@app.get("/setup")
def setup_page(request: Request):
    if db.user_count():
        return back("/login")
    return render(request, "setup.html", error=None)


@app.post("/setup")
async def setup_submit(request: Request):
    if db.user_count():
        return back("/login")
    form = await request.form()
    username, password = form.get("username", "").strip(), form.get("password", "")
    if not username or len(password) < 8:
        return render(request, "setup.html", error="Pick a username and a password of at least 8 characters.")
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                           (username, auth.hash_password(password)))
        conn.commit()
        request.session.update(uid=cur.lastrowid, username=username)
    finally:
        conn.close()
    return back("/accounts?welcome=1")


@app.get("/login")
def login_page(request: Request):
    if db.user_count() == 0:
        return back("/setup")
    return render(request, "login.html", error=None)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username, password = form.get("username", "").strip(), form.get("password", "")
    conn = get_conn()
    try:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    if not user or not auth.verify_password(password, user["password_hash"]):
        return render(request, "login.html", error="Wrong username or password.")
    request.session.clear()
    request.session.update(uid=user["id"], username=user["username"])
    return back("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------- budget math

def category_totals(conn, month: str) -> list[dict]:
    start, end = month_range(month)
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.group_name, c.kind,
               COALESCE(b.amount_cents, prev.amount_cents, 0) AS budget,
               b.amount_cents IS NOT NULL AS set_here,
               CASE WHEN b.amount_cents IS NULL THEN prev.month END AS carried_from,
               COALESCE(t.total, 0) AS total, COALESCE(t.n, 0) AS n
        FROM categories c
        LEFT JOIN budgets prev ON prev.category_id = c.id AND prev.month = (
            SELECT MAX(month) FROM budgets p WHERE p.category_id = c.id AND p.month < ?)
        LEFT JOIN budgets b ON b.category_id = c.id AND b.month = ?
        LEFT JOIN (SELECT category_id, SUM(amount_cents) AS total, COUNT(*) AS n
                   FROM transactions WHERE date >= ? AND date < ? GROUP BY category_id) t
               ON t.category_id = c.id
        ORDER BY (SELECT MIN(position) FROM categories g WHERE g.group_name = c.group_name),
                 c.position, c.name
        """,
        (month, month, start, end),
    ).fetchall()
    cats = [dict(r) for r in rows]
    for c in cats:
        c["spent"] = -c["total"] if c["kind"] == "expense" else 0
        c["remaining"] = c["budget"] - c["spent"]
        c["pct"] = min(100, round(c["spent"] * 100 / c["budget"])) if c["budget"] > 0 else 0
    return cats


def summarize(conn, month: str) -> dict:
    cats = category_totals(conn, month)
    start, end = month_range(month)
    uncat = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS spent,
                  COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS income,
                  COUNT(*) AS n
           FROM transactions WHERE category_id IS NULL AND date >= ? AND date < ?""",
        (start, end),
    ).fetchone()
    expense = [c for c in cats if c["kind"] == "expense"]
    income = sum(c["total"] for c in cats if c["kind"] == "income") + uncat["income"]
    spent = sum(c["spent"] for c in expense) + uncat["spent"]
    budgeted = sum(c["budget"] for c in expense)
    return {
        "cats": cats, "expense": expense, "income": income, "spent": spent,
        "net": income - spent, "budgeted": budgeted, "left": budgeted - spent,
        "uncat": dict(uncat),
    }


def grouped(cats: list[dict]) -> list[tuple[str, list[dict]]]:
    return [(g, list(items)) for g, items in itertools.groupby(cats, key=lambda c: c["group_name"])]


# ---------------------------------------------------------------- dashboard

@app.get("/")
def dashboard(request: Request, month: Optional[str] = None):
    month = valid_month(month)
    conn = get_conn()
    try:
        s = summarize(conn, month)
        recent = conn.execute(
            """SELECT t.*, c.name AS category, a.name AS account FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id JOIN accounts a ON a.id = t.account_id
               ORDER BY t.date DESC, t.id DESC LIMIT 8"""
        ).fetchall()
        has_data = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None
        summary = conn.execute("SELECT * FROM ai_summaries WHERE month = ?", (month,)).fetchone()
        debt_plan = snowball.plan(conn)
        bank = brands.account_brands(conn)
        worth = networth.summary(conn)
        networth.record(conn, worth)
        trend = []
        for back_n in range(11, -1, -1):
            m = shift_month(month, -back_n)
            summary_m = summarize(conn, m)
            trend.append({"label": date(int(m[:4]), int(m[5:]), 1).strftime("%b %Y"),
                          "income": summary_m["income"], "spent": summary_m["spent"]})
    finally:
        conn.close()
    tracked = [c for c in s["expense"] if c["budget"] > 0 or c["spent"] > 0]
    tracked.sort(key=lambda c: (c["budget"] == 0, -c["spent"]))
    return render(request, "dashboard.html", month=month, prev=shift_month(month, -1),
                  next=shift_month(month, 1), s=s, tracked=tracked, recent=recent, has_data=has_data,
                  summary=summary, ai_error=request.query_params.get("ai_error", ""), plan=debt_plan,
                  trend=trend, brands=bank, worth=worth)


# ---------------------------------------------------------------- transactions

def all_categories(conn):
    return conn.execute(
        """SELECT * FROM categories c ORDER BY
           (SELECT MIN(position) FROM categories g WHERE g.group_name = c.group_name), position, name"""
    ).fetchall()


@app.get("/transactions")
def transactions(request: Request, month: Optional[str] = None, account: str = "", category: str = "",
                 q: str = "", batch: str = ""):
    month = "all" if month == "all" else valid_month(month)
    where, args = [], []
    if month != "all":
        start, end = month_range(month)
        where.append("t.date >= ? AND t.date < ?")
        args += [start, end]
    if account.isdigit():
        where.append("t.account_id = ?")
        args.append(int(account))
    if category == "none":
        where.append("t.category_id IS NULL")
    elif category.isdigit():
        where.append("t.category_id = ?")
        args.append(int(category))
    if q.strip():
        where.append("(t.description LIKE ? OR t.notes LIKE ?)")
        args += [f"%{q.strip()}%"] * 2
    if batch.isdigit():
        where.append("t.import_id = ?")
        args.append(int(batch))
    sql = """SELECT t.*, a.name AS account, pa.name AS pair_account FROM transactions t
             JOIN accounts a ON a.id = t.account_id
             LEFT JOIN transactions p ON p.id = t.pair_id LEFT JOIN accounts pa ON pa.id = p.account_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT 2000"
    conn = get_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
        cats = all_categories(conn)
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        bank = brands.account_brands(conn)
    finally:
        conn.close()
    total = sum(r["amount_cents"] for r in rows)
    return render(request, "transactions.html", rows=rows, cats=grouped([dict(c) for c in cats]),
                  accounts=accounts, month=month, account=account, category=category, q=q, batch=batch,
                  total=total, prev=shift_month(month, -1) if month != "all" else None,
                  next=shift_month(month, 1) if month != "all" else None, today=date.today().isoformat(),
                  brands=bank)


@app.post("/api/transactions/{tx_id}")
async def update_transaction(tx_id: int, request: Request):
    data = await request.json()
    conn = get_conn()
    try:
        tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not tx:
            return JSONResponse({"error": "not found"}, status_code=404)
        result = {"ok": True}
        if "category_id" in data:
            cid = data["category_id"]
            cid = int(cid) if cid not in (None, "") else None
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (cid, tx_id))
            if cid is not None:
                pattern = importers.rule_suggestion(tx["description"])
                similar = conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND id != ? AND description LIKE ?",
                    (tx_id, f"%{pattern}%"),
                ).fetchone()[0]
                result.update(pattern=pattern, similar=similar)
        if "notes" in data:
            conn.execute("UPDATE transactions SET notes = ? WHERE id = ?", (str(data["notes"])[:500], tx_id))
        conn.commit()
        return result
    finally:
        conn.close()


@app.post("/transactions/new")
async def add_transaction(request: Request):
    form = await request.form()
    when = importers.parse_date(form.get("date", ""))
    amount = importers.parse_amount(form.get("amount", ""))
    desc = form.get("description", "").strip()
    account = form.get("account_id", "")
    if not (when and amount is not None and desc and account.isdigit()):
        return back("/transactions?error=1")
    if form.get("direction") == "out":
        amount = -abs(amount)
    else:
        amount = abs(amount)
    cid = form.get("category_id") or None
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO transactions (account_id, date, description, amount_cents, category_id) VALUES (?, ?, ?, ?, ?)",
            (int(account), when, desc, amount, int(cid) if cid else None),
        )
        conn.commit()
    finally:
        conn.close()
    return back(f"/transactions?month={when[:7]}")


@app.post("/transactions/{tx_id}/delete")
def delete_transaction(tx_id: int, request: Request):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
    finally:
        conn.close()
    return back(request.headers.get("referer") or "/transactions")


# ---------------------------------------------------------------- import

def upload_path(token: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        raise ValueError("bad token")
    return os.path.join(db.DATA_DIR, "uploads", token + ".csv")


@app.get("/import")
def import_page(request: Request, done: str = "", error: str = ""):
    conn = get_conn()
    try:
        accounts = conn.execute(
            """SELECT a.*, EXISTS (SELECT 1 FROM plaid_accounts p WHERE p.account_id = a.id) AS synced
               FROM accounts a ORDER BY name""").fetchall()
        history = conn.execute(
            """SELECT i.*, a.name AS account, u.username FROM imports i
               JOIN accounts a ON a.id = i.account_id LEFT JOIN users u ON u.id = i.user_id
               ORDER BY i.id DESC LIMIT 30"""
        ).fetchall()
        result = conn.execute("SELECT * FROM imports WHERE id = ?", (int(done),)).fetchone() if done.isdigit() else None
    finally:
        conn.close()
    return render(request, "import.html", accounts=accounts, history=history, result=result, error=error)


@app.post("/import")
async def import_upload(request: Request):
    form = await request.form()
    upload, account = form.get("file"), form.get("account_id", "")
    if not account.isdigit() or upload is None or not getattr(upload, "filename", ""):
        return back("/import?error=Choose+an+account+and+a+file.")
    raw = await upload.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return back("/import?error=That+file+is+too+big.")
    text = importers.decode(raw)
    conn = get_conn()
    try:
        acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account),)).fetchone()
        if not acct:
            return back("/import?error=Unknown+account.")
        if importers.is_ofx(text):
            txs = importers.parse_ofx(text)
            if not txs:
                return back("/import?error=No+transactions+found+in+that+file.")
            import_id = ledger.import_file(conn, acct["id"], txs, upload.filename, request.session["uid"])
            return back(f"/import?done={import_id}")
    finally:
        conn.close()

    header, rows = importers.read_csv(text)
    if not header or not rows:
        return back("/import?error=Couldn't+read+that+file.+Try+a+CSV,+OFX+or+QFX+export.")
    token = secrets.token_hex(8)
    with open(upload_path(token), "w", encoding="utf-8") as f:
        f.write(text)
    saved = json.loads(acct["csv_mapping"]) if acct["csv_mapping"] else None
    if saved and saved.get("header") == header:
        mapping = saved["mapping"]
    else:
        mapping = importers.guess_mapping(header, rows, acct["kind"])
    return render_mapping(request, acct, token, upload.filename, header, rows, mapping)


def render_mapping(request, acct, token, filename, header, rows, mapping, error=None):
    ready = mapping["date"] is not None and mapping["description"] is not None and (
        mapping["amount"] is not None or mapping["debit"] is not None or mapping["credit"] is not None)
    txs, bad = importers.apply_mapping(rows, mapping) if ready else ([], len(rows))
    return render(request, "import_map.html", acct=acct, token=token, filename=filename, header=header,
                  sample=rows[:5], mapping=mapping, preview=txs[:25], count=len(txs), bad=bad,
                  ready=ready and bool(txs), error=error,
                  spent=sum(t["amount_cents"] for t in txs if t["amount_cents"] < 0),
                  received=sum(t["amount_cents"] for t in txs if t["amount_cents"] > 0))


@app.post("/import/map")
async def import_map(request: Request):
    form = await request.form()
    try:
        path = upload_path(form.get("token", ""))
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (ValueError, OSError):
        return back("/import?error=That+upload+expired.+Please+upload+it+again.")

    def col(name):
        value = form.get(name, "")
        return int(value) if value.isdigit() else None

    mapping = {k: col(k) for k in ("date", "description", "amount", "debit", "credit")}
    mapping["flip"] = form.get("flip") == "1"
    if mapping["amount"] is not None:
        mapping["debit"] = mapping["credit"] = None
    header, rows = importers.read_csv(text)
    filename = form.get("filename", "upload.csv")
    conn = get_conn()
    try:
        acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(form.get("account_id", 0)),)).fetchone()
        if not acct:
            return back("/import?error=Unknown+account.")
        if form.get("action") != "import":
            return render_mapping(request, acct, form["token"], filename, header, rows, mapping)
        txs, _ = importers.apply_mapping(rows, mapping) if None not in (mapping["date"], mapping["description"]) else ([], 0)
        if not txs:
            return render_mapping(request, acct, form["token"], filename, header, rows, mapping,
                                  error="Nothing to import with these columns.")
        conn.execute("UPDATE accounts SET csv_mapping = ? WHERE id = ?",
                     (json.dumps({"header": header, "mapping": mapping}), acct["id"]))
        import_id = ledger.import_file(conn, acct["id"], txs, filename, request.session["uid"])
    finally:
        conn.close()
    os.remove(path)
    return back(f"/import?done={import_id}")


@app.post("/import/{import_id}/undo")
def undo_import(import_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))  # cascades to its transactions
        conn.commit()
    finally:
        conn.close()
    return back("/import")


# ---------------------------------------------------------------- budget

@app.get("/budget")
def budget_page(request: Request, month: Optional[str] = None, saved: str = ""):
    month = valid_month(month)
    conn = get_conn()
    try:
        s = summarize(conn, month)
        avg_start, avg_end = month_range(shift_month(month, -3))[0], month_range(month)[0]
        avgs = {r[0]: -r[1] / 3 for r in conn.execute(
            "SELECT category_id, SUM(amount_cents) FROM transactions WHERE date >= ? AND date < ? GROUP BY category_id",
            (avg_start, avg_end))}
        has_prev = conn.execute("SELECT 1 FROM budgets WHERE month = ? LIMIT 1", (shift_month(month, -1),)).fetchone()
    finally:
        conn.close()
    for c in s["expense"]:
        c["avg"] = round(avgs.get(c["id"], 0))
    return render(request, "budget.html", month=month, prev=shift_month(month, -1), next=shift_month(month, 1),
                  s=s, groups=grouped(s["expense"]), has_prev=bool(has_prev), saved=saved)


@app.post("/budget")
async def budget_save(request: Request):
    form = await request.form()
    month = valid_month(form.get("month"))
    conn = get_conn()
    try:
        for key, value in form.items():
            if not key.startswith("b_") or not key[2:].isdigit():
                continue
            cents = importers.parse_amount(value) if value.strip() else None
            if cents is not None:
                conn.execute(
                    """INSERT INTO budgets (category_id, month, amount_cents) VALUES (?, ?, ?)
                       ON CONFLICT (category_id, month) DO UPDATE SET amount_cents = excluded.amount_cents""",
                    (int(key[2:]), month, abs(cents)),
                )
            else:
                conn.execute("DELETE FROM budgets WHERE category_id = ? AND month = ?", (int(key[2:]), month))
        conn.commit()
    finally:
        conn.close()
    return back(f"/budget?month={month}&saved=1")


@app.post("/budget/copy")
async def budget_copy(request: Request):
    form = await request.form()
    month = valid_month(form.get("month"))
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO budgets (category_id, month, amount_cents)
               SELECT category_id, ?, amount_cents FROM budgets WHERE month = ?""",
            (month, shift_month(month, -1)),
        )
        conn.commit()
    finally:
        conn.close()
    return back(f"/budget?month={month}")


# ---------------------------------------------------------------- categories & rules

@app.get("/categories")
def categories_page(request: Request, applied: str = ""):
    conn = get_conn()
    try:
        cats = all_categories(conn)
        counts = dict(conn.execute("SELECT category_id, COUNT(*) FROM transactions GROUP BY category_id").fetchall())
        rules = conn.execute(
            "SELECT r.*, c.name AS category FROM rules r JOIN categories c ON c.id = r.category_id ORDER BY r.pattern"
        ).fetchall()
    finally:
        conn.close()
    cats = [dict(c, n=counts.get(c["id"], 0)) for c in cats]
    groups = sorted({c["group_name"] for c in cats})
    return render(request, "categories.html", groups=grouped(cats), group_names=groups, rules=rules,
                  applied=applied)


@app.post("/categories")
async def category_add(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    group = form.get("group_name", "").strip() or "Other"
    kind = form.get("kind") if form.get("kind") in ("expense", "income", "transfer") else "expense"
    if name:
        conn = get_conn()
        try:
            pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM categories").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO categories (name, group_name, kind, position) VALUES (?, ?, ?, ?)",
                         (name, group, kind, pos))
            conn.commit()
        finally:
            conn.close()
    return back("/categories")


@app.post("/categories/{cat_id}")
async def category_edit(cat_id: int, request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        if form.get("action") == "delete":
            conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        else:
            kind = form.get("kind") if form.get("kind") in ("expense", "income", "transfer") else "expense"
            conn.execute("UPDATE categories SET name = ?, group_name = ?, kind = ? WHERE id = ?",
                         (form.get("name", "").strip() or "Unnamed", form.get("group_name", "").strip() or "Other",
                          kind, cat_id))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # duplicate name; leave it unchanged
    finally:
        conn.close()
    return back("/categories")


@app.post("/rules")
async def rule_add(request: Request):
    form = await request.form()
    pattern, cid = form.get("pattern", "").strip(), form.get("category_id", "")
    if pattern and cid.isdigit():
        conn = get_conn()
        try:
            conn.execute("INSERT INTO rules (pattern, category_id) VALUES (?, ?)", (pattern, int(cid)))
            conn.commit()
            changed = ledger.apply_rules_to_uncategorized(conn)
        finally:
            conn.close()
        return back(f"/categories?applied={changed}")
    return back("/categories")


@app.post("/api/rules")
async def rule_add_api(request: Request):
    data = await request.json()
    pattern, cid = str(data.get("pattern", "")).strip(), data.get("category_id")
    if not pattern or not str(cid).isdigit():
        return JSONResponse({"error": "pattern and category required"}, status_code=400)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO rules (pattern, category_id) VALUES (?, ?)", (pattern, int(cid)))
        conn.commit()
        return {"ok": True, "changed": ledger.apply_rules_to_uncategorized(conn)}
    finally:
        conn.close()


@app.post("/rules/apply")
def rules_apply():
    conn = get_conn()
    try:
        changed = ledger.apply_rules_to_uncategorized(conn)
    finally:
        conn.close()
    return back(f"/categories?applied={changed}")


@app.post("/rules/{rule_id}/delete")
def rule_delete(rule_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()
    return back("/categories")


# ---------------------------------------------------------------- accounts

@app.get("/accounts")
def accounts_page(request: Request, welcome: str = ""):
    conn = get_conn()
    try:
        accounts = conn.execute(
            """SELECT a.*, COUNT(t.id) AS n, MIN(t.date) AS first, MAX(t.date) AS last,
                      EXISTS (SELECT 1 FROM plaid_accounts p WHERE p.account_id = a.id) AS synced
               FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
               GROUP BY a.id ORDER BY a.name"""
        ).fetchall()
    finally:
        conn.close()
    conn = get_conn()
    try:
        bank = brands.account_brands(conn)
    finally:
        conn.close()
    return render(request, "accounts.html", accounts=accounts, kinds=ACCOUNT_KINDS, welcome=welcome, brands=bank)


@app.post("/accounts")
async def account_add(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    kind = form.get("kind") if form.get("kind") in ACCOUNT_KINDS else "checking"
    if name:
        conn = get_conn()
        try:
            conn.execute("INSERT INTO accounts (name, kind) VALUES (?, ?)", (name, kind))
            conn.commit()
        finally:
            conn.close()
    return back("/accounts")


@app.post("/accounts/{account_id}")
async def account_edit(account_id: int, request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        if form.get("action") == "delete":
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        else:
            kind = form.get("kind") if form.get("kind") in ACCOUNT_KINDS else "checking"
            conn.execute("UPDATE accounts SET name = ?, kind = ? WHERE id = ?",
                         (form.get("name", "").strip() or "Unnamed", kind, account_id))
            color = brands.valid_color(form.get("color", ""))
            if form.get("action") == "reset_color":
                conn.execute("UPDATE accounts SET color = NULL WHERE id = ?", (account_id,))
            elif color and color != form.get("color_was"):  # only when the picker was actually changed
                same_as_bank = color == brands.valid_color(form.get("bank_color", ""))
                conn.execute("UPDATE accounts SET color = ? WHERE id = ?", (None if same_as_bank else color, account_id))
        conn.commit()
    finally:
        conn.close()
    return back("/accounts")


# ---------------------------------------------------------------- bank sync (Plaid)

@app.get("/connections")
def connections_page(request: Request, msg: str = "", error: str = "", setup: str = ""):
    config = bank_sync.load_config()
    conn = get_conn()
    try:
        items = [dict(i) for i in conn.execute("SELECT * FROM plaid_items ORDER BY institution").fetchall()]
        for item in items:
            item["accounts"] = conn.execute(
                """SELECT p.*, a.balance_cents FROM plaid_accounts p LEFT JOIN accounts a ON a.id = p.account_id
                   WHERE p.item_id = ? ORDER BY p.type, p.name""", (item["id"],)).fetchall()
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    finally:
        conn.close()
    return render(request, "connections.html", config=config, items=items, accounts=accounts,
                  msg=msg, error=error, setup=setup)


@app.get("/bank-logo/{item_id}")
def bank_logo(item_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT logo FROM plaid_items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["logo"]:
        return Response(status_code=404)
    return Response(base64.b64decode(row["logo"]), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=604800"})


@app.post("/connections/{item_id}/color")
async def connection_color(item_id: int, request: Request):
    form = await request.form()
    color = brands.valid_color(form.get("color", ""))
    if color:
        conn = get_conn()
        try:
            conn.execute("UPDATE plaid_items SET color = ? WHERE id = ?", (color, item_id))
            conn.commit()
        finally:
            conn.close()
    return back(f"/connections#item-{item_id}")


@app.post("/connections/keys")
async def connections_keys(request: Request):
    form = await request.form()
    old = bank_sync.load_config() or {}
    client_id = form.get("client_id", "").strip() or old.get("client_id", "")
    secret = form.get("secret", "").strip() or old.get("secret", "")
    env = form.get("env") if form.get("env") in bank_sync.HOSTS else "production"
    if not client_id or not secret:
        return back("/connections?error=Enter+both+the+client+ID+and+the+secret.")
    try:
        bank_sync.link_token(config={"client_id": client_id, "secret": secret, "env": env})
    except bank_sync.PlaidError as e:
        return back("/connections?" + urlencode({"error": f"Plaid didn't accept those keys: {e}"}))
    bank_sync.save_config(client_id, secret, env)
    return back("/connections?msg=Plaid+keys+saved.")


@app.post("/api/plaid/link-token")
async def plaid_link_token(request: Request):
    data = await request.json()
    access_token = None
    if data.get("item"):
        conn = get_conn()
        try:
            row = conn.execute("SELECT access_token FROM plaid_items WHERE id = ?", (int(data["item"]),)).fetchone()
        finally:
            conn.close()
        if not row:
            return JSONResponse({"error": "Unknown connection"}, status_code=404)
        access_token = row["access_token"]
    try:
        return {"link_token": bank_sync.link_token(access_token)}
    except bank_sync.PlaidError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/plaid/exchange")
async def plaid_exchange(request: Request):
    data = await request.json()
    try:
        item_id = bank_sync.add_item(data["public_token"], (data.get("institution") or {}).get("name", ""))
    except bank_sync.PlaidError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"redirect": f"/connections?setup={item_id}#item-{item_id}"}


@app.post("/api/plaid/reconnected/{item_id}")
def plaid_reconnected(item_id: int):
    conn = get_conn()
    try:
        conn.execute("UPDATE plaid_items SET status = 'ok', error = '' WHERE id = ? AND status != 'new'", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return {"redirect": sync_redirect(item_id)}


def sync_redirect(item_id: int) -> str:
    try:
        result = bank_sync.sync_item(item_id)
    except bank_sync.PlaidError as e:
        return "/connections?" + urlencode({"error": str(e)})
    note = f"Synced: {result['added']} new transaction{'s' if result['added'] != 1 else ''}."
    if result["added"] == 0 and result["first"]:
        note += " A brand-new connection can take a few minutes before the bank's history is ready."
    return "/connections?" + urlencode({"msg": note})


@app.post("/connections/{item_id}/accounts")
async def connection_accounts(item_id: int, request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        item = conn.execute("SELECT * FROM plaid_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return back("/connections")
        for p in conn.execute("SELECT * FROM plaid_accounts WHERE item_id = ?", (item_id,)).fetchall():
            choice = form.get(f"acct_{p['id']}", "")
            if choice == "new" and p["account_id"]:
                target = p["account_id"]  # already has one (e.g. a repeated submit); don't make a duplicate
            elif choice == "new":
                kind = bank_sync.account_kind(dict(p))
                name = f"{p['name']} ••{p['mask']}" if p["mask"] else p["name"]
                target = conn.execute("INSERT INTO accounts (name, kind) VALUES (?, ?)", (name, kind)).lastrowid
            elif choice.isdigit():
                target = int(choice)
            else:
                target = None
            conn.execute("UPDATE plaid_accounts SET account_id = ? WHERE id = ?", (target, p["id"]))
        if item["status"] == "new":
            conn.execute("UPDATE plaid_items SET status = 'ok' WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return back(await run_in_threadpool(sync_redirect, item_id))


@app.post("/connections/{item_id}/sync")
def connection_sync(item_id: int):
    return back(sync_redirect(item_id))


@app.post("/connections/sync")
def connections_sync_all():
    result = bank_sync.sync_all()
    if result["errors"]:
        return back("/connections?" + urlencode({"error": "; ".join(result["errors"])}))
    return back("/connections?" + urlencode({"msg": f"Synced: {result['added']} new transactions."}))


@app.post("/connections/{item_id}/delete")
def connection_delete(item_id: int):
    bank_sync.remove_item(item_id)
    return back("/connections?msg=Disconnected.+Its+accounts+and+transactions+are+still+here.")


# ---------------------------------------------------------------- daily (quick budget view)

def budget_state(budget: int, left: int, used: float, pace: float) -> str:
    """ok, fast (spent noticeably more of the budget than of the month) or over."""
    if budget <= 0:
        return "none"
    if left < 0:
        return "over"
    return "fast" if used > pace + 0.10 else "ok"


@app.get("/daily")
def daily_page(request: Request, month: Optional[str] = None):
    month = valid_month(month)
    y, m = map(int, month.split("-"))
    days_in_month = (date(y + (m == 12), m % 12 + 1, 1) - date(y, m, 1)).days
    today = date.today()
    current, past = month == today.strftime("%Y-%m"), month < today.strftime("%Y-%m")
    day = today.day if current else (days_in_month if past else 0)
    days_left = days_in_month - day + 1 if current else (0 if past else days_in_month)
    pace = day / days_in_month
    conn = get_conn()
    try:
        s = summarize(conn, month)
    finally:
        conn.close()

    def row(c):
        left = c["budget"] - c["spent"]
        return dict(c, left=left,
                    used_pct=min(100, round(c["spent"] * 100 / c["budget"])) if c["budget"] > 0 else 0,
                    left_pct=max(0, min(100, round(left * 100 / c["budget"]))) if c["budget"] > 0 else 0,
                    state=budget_state(c["budget"], left, c["spent"] / c["budget"] if c["budget"] > 0 else 0, pace),
                    per_day=left // days_left if days_left and left > 0 else 0)
    budgeted = [row(c) for c in s["expense"] if c["budget"] > 0]
    unbudgeted = [c for c in s["expense"] if c["budget"] <= 0 and c["spent"] > 0]
    total = row({"budget": sum(c["budget"] for c in budgeted), "spent": sum(c["spent"] for c in budgeted)})
    return render(request, "daily.html", month=month, prev=shift_month(month, -1), next=shift_month(month, 1),
                  groups=grouped(budgeted), unbudgeted=unbudgeted, uncat=s["uncat"], total=total,
                  current=current, past=past, day=day, days_in_month=days_in_month, days_left=days_left,
                  month_pct=round(pace * 100, 1))


# ---------------------------------------------------------------- debt snowball

@app.get("/snowball")
def snowball_page(request: Request, msg: str = ""):
    conn = get_conn()
    try:
        p = snowball.plan(conn)
        snowball.record_balances(conn)
        bank = brands.account_brands(conn)
        for d in p["paid_off"]:
            d["paid_off_on"] = snowball.first_paid_off_date(conn, d["id"])
    finally:
        conn.close()
    return render(request, "snowball.html", p=p, msg=msg, brands=bank)


def cents_or_none(value: str) -> Optional[int]:
    parsed = importers.parse_amount(value or "")
    return abs(parsed) if parsed is not None else None


@app.post("/snowball/extra")
async def snowball_extra(request: Request):
    form = await request.form()
    mode = "total" if form.get("mode") == "total" else "extra"
    amount = cents_or_none(form.get("amount", form.get("extra", ""))) or 0
    conn = get_conn()
    try:
        snowball.set_setting(conn, "snowball_mode", mode)
        snowball.set_setting(conn, "snowball_total_cents" if mode == "total" else "snowball_extra_cents", str(amount))
        conn.commit()
    finally:
        conn.close()
    page = "/settings" if form.get("next") == "settings" else "/snowball"
    return back(f"{page}?msg=Snowball+amount+saved.#snowball-amount")


@app.post("/snowball/debts")
async def snowball_debts_save(request: Request):
    """Save the details table: min payment, APR, starting balance, balance (hand-entered debts)."""
    form = await request.form()
    conn = get_conn()
    try:
        for d in conn.execute("SELECT * FROM debts").fetchall():
            i = d["id"]
            if f"min_{i}" not in form:
                continue
            apr = form.get(f"apr_{i}", "").replace("%", "").strip()
            try:
                apr = max(0.0, min(100.0, float(apr))) if apr else 0.0
            except ValueError:
                apr = d["apr"]
            fields = {"min_payment_cents": cents_or_none(form.get(f"min_{i}", "")) or 0, "apr": apr}
            if f"pm_{i}" in form:
                fields["payment_match"] = form.get(f"pm_{i}", "").strip()[:60]
            start = cents_or_none(form.get(f"start_{i}", ""))
            if start is not None:
                fields["start_balance_cents"] = start
            if f"bal_{i}" in form:
                fields["balance_cents"] = cents_or_none(form.get(f"bal_{i}", ""))
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE debts SET {sets} WHERE id = ?", (*fields.values(), i))
        conn.commit()
        ledger.mark_debt_payments(conn)
    finally:
        conn.close()
    return back("/snowball?msg=Saved.")


@app.post("/snowball/debts/new")
async def snowball_debt_add(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    balance = cents_or_none(form.get("balance", ""))
    if not name or balance is None:
        return back("/snowball?msg=Give+the+debt+a+name+and+a+balance.")
    apr = form.get("apr", "").replace("%", "").strip()
    try:
        apr = float(apr) if apr else 0.0
    except ValueError:
        apr = 0.0
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO debts (name, lender, balance_cents, apr, min_payment_cents, start_balance_cents, start_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, form.get("lender", "").strip()[:60], balance, apr, cents_or_none(form.get("min", "")) or 0,
             balance, date.today().isoformat()))
        conn.commit()
    finally:
        conn.close()
    return back("/snowball?msg=Added+" + urlencode({"": name})[1:] + ".")


@app.post("/snowball/import")
async def snowball_import(request: Request):
    """Read a loan servicer's statement (PDF or pasted text) and show what would be imported."""
    form = await request.form()
    upload, text = form.get("file"), form.get("text", "")
    if upload is not None and getattr(upload, "filename", ""):
        raw = await upload.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            return back("/snowball?msg=That+file+is+too+big.")
        try:
            text = snowball.pdf_text(raw) if raw[:5] == b"%PDF-" else importers.decode(raw)
        except Exception:
            return back("/snowball?msg=Couldn't+read+that+PDF.")
    found = snowball.parse_loan_statement(text or "")
    if not found["loans"]:
        return back("/snowball?msg=No+loans+found.+Look+for+the+servicer's+%22all+loan+details%22+page,+"
                    "or+paste+lines+like+%22Loan+name+%241,234.56+6.5%25%22.")
    group = (form.get("group", "").strip() or found["servicer"] or "Imported loans")[:40]
    conn = get_conn()
    try:
        existing = {d["name"].lower(): d for d in snowball.load_debts(conn)
                    if d["group_name"] == group and not d["account_id"]}
    finally:
        conn.close()
    for loan in found["loans"]:
        loan["existing"] = existing.pop(loan["name"].lower(), None)
    return render(request, "snowball_import.html", loans=found["loans"], missing=list(existing.values()),
                  group=group, as_of=found["as_of"], total=sum(l["balance"] for l in found["loans"]))


@app.post("/snowball/import/apply")
async def snowball_import_apply(request: Request):
    form = await request.form()
    group = form.get("group", "").strip()[:40] or "Imported loans"
    today = date.today().isoformat()
    added = updated = removed = 0
    conn = get_conn()
    try:
        current = {r["name"].lower(): r for r in conn.execute(
            "SELECT * FROM debts WHERE group_name = ? AND account_id IS NULL", (group,))}
        for i in range(int(form.get("count", "0") or 0)):
            if not form.get(f"use_{i}"):
                continue
            name = form.get(f"name_{i}", "").strip()[:80]
            try:
                balance, apr = int(form.get(f"bal_{i}", "")), float(form.get(f"apr_{i}", "0"))
                info = json.loads(form.get(f"info_{i}", "") or "{}")
            except ValueError:
                continue
            details = json.dumps(info) if info else ""
            if name.lower() in current:
                old = current[name.lower()]
                conn.execute("UPDATE debts SET balance_cents = ?, apr = ?, details = ? WHERE id = ?",
                             (balance, apr, details or old["details"], old["id"]))
                updated += 1
            elif name:
                conn.execute(
                    """INSERT INTO debts (name, balance_cents, apr, start_balance_cents, start_date, group_name, details)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""", (name, balance, apr, balance, today, group, details))
                added += 1
        for key in form.keys():
            if key.startswith("remove_") and key[7:].isdigit():
                removed += conn.execute("DELETE FROM debts WHERE id = ? AND account_id IS NULL AND group_name = ?",
                                        (int(key[7:]), group)).rowcount
        monthly = cents_or_none(form.get("monthly", ""))
        if monthly:
            rows = conn.execute("SELECT id, balance_cents FROM debts WHERE group_name = ? AND account_id IS NULL",
                                (group,)).fetchall()
            total = sum(r["balance_cents"] or 0 for r in rows) or 1
            left = monthly
            for n, r in enumerate(rows):
                share = left if n == len(rows) - 1 else round(monthly * (r["balance_cents"] or 0) / total)
                left -= share
                conn.execute("UPDATE debts SET min_payment_cents = ? WHERE id = ?", (share, r["id"]))
        conn.commit()
        snowball.record_balances(conn)
    finally:
        conn.close()
    parts = [f"{n} {word}" for n, word in ((added, "added"), (updated, "updated"), (removed, "removed")) if n]
    return back("/snowball?" + urlencode({"msg": f"{group}: " + (", ".join(parts) or "nothing changed") + "."}))


@app.post("/snowball/groups/consolidate")
async def snowball_consolidate(request: Request):
    """Replace a group's loans with the one consolidation loan, keeping the progress made so far."""
    form = await request.form()
    group = form.get("group", "")
    conn = get_conn()
    try:
        old = [d for d in snowball.load_debts(conn) if d["group_name"] == group and not d["account_id"]]
        if not old:
            return back("/snowball")
        balance = cents_or_none(form.get("balance", ""))
        if balance is None:
            balance = sum(d["balance"] for d in old)
        try:
            apr = float(form.get("apr", "").replace("%", "") or snowball.consolidated_rate(old))
        except ValueError:
            apr = snowball.consolidated_rate(old)
        name = form.get("name", "").strip()[:80] or f"{group} Consolidation"
        conn.execute(
            """INSERT INTO debts (name, balance_cents, apr, min_payment_cents, start_balance_cents, start_date,
                                  group_name, included)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, balance, apr, cents_or_none(form.get("min", "")) or 0, sum(d["start"] for d in old),
             min(d["start_date"] for d in old), group, max(d["included"] for d in old)))
        conn.executemany("DELETE FROM debts WHERE id = ?", [(d["id"],) for d in old])
        conn.commit()
        snowball.record_balances(conn)
    finally:
        conn.close()
    return back("/snowball?" + urlencode({"msg": f"Replaced {len(old)} {group} loans with {name}."}))


@app.post("/snowball/groups/remove")
async def snowball_group_remove(request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM debts WHERE group_name = ? AND account_id IS NULL",
                         (form.get("group", ""),)).rowcount
        conn.commit()
    finally:
        conn.close()
    return back("/snowball?" + urlencode({"msg": f"Removed {n} loan{'s' if n != 1 else ''}."}))


@app.post("/snowball/debts/{debt_id}/{action}")
def snowball_debt_action(debt_id: int, action: str):
    conn = get_conn()
    try:
        modes = {"include": 1, "deprioritize": 2, "exclude": 0}
        if action in modes:
            conn.execute("UPDATE debts SET included = ? WHERE id = ?", (modes[action], debt_id))
        elif action == "delete":
            conn.execute("DELETE FROM debts WHERE id = ? AND account_id IS NULL", (debt_id,))
        conn.commit()
    finally:
        conn.close()
    return back("/snowball")


# ---------------------------------------------------------------- Claude

@app.get("/ask")
def ask_page(request: Request, chat: str = ""):
    conn = get_conn()
    try:
        chats = conn.execute(
            """SELECT c.id, c.title, c.updated_at, u.username FROM ai_chats c
               LEFT JOIN users u ON u.id = c.user_id ORDER BY c.updated_at DESC LIMIT 20""").fetchall()
        history = ai.chat_history(conn, int(chat)) if chat.isdigit() else []
    finally:
        conn.close()
    return render(request, "ask.html", chats=chats, history=history, chat_id=chat if history else "")


@app.post("/api/ask")
async def ask_api(request: Request):
    data = await request.json()
    question = str(data.get("question", "")).strip()[:2000]
    if not question:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    chat_id = data.get("chat_id")
    chat_id = int(chat_id) if str(chat_id or "").isdigit() else None
    uid = request.session["uid"]

    def work():
        conn = get_conn()
        try:
            return ai.ask(conn, chat_id, question, uid)
        finally:
            conn.close()
    try:
        new_id, answer, cost = await run_in_threadpool(work)
    except Exception as e:  # shown to the user in the chat
        return JSONResponse({"error": ai.friendly_error(e)}, status_code=502)
    return {"chat_id": new_id, "html": answer, "cost": f"{cost:.1f}¢"}


@app.post("/ask/{chat_id}/delete")
def ask_delete(chat_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM ai_chats WHERE id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()
    return back("/ask")


@app.post("/ai/summary")
async def ai_summary(request: Request):
    form = await request.form()
    month = valid_month(form.get("month"))
    uid = request.session["uid"]

    def work():
        conn = get_conn()
        try:
            ai.summarize_month(conn, month, month_label(month), uid)
        finally:
            conn.close()
    try:
        await run_in_threadpool(work)
    except Exception as e:
        return back(f"/?month={month}&" + urlencode({"ai_error": ai.friendly_error(e)}))
    return back(f"/?month={month}#summary")


@app.post("/ai/categorize")
async def ai_categorize(request: Request):
    form = await request.form()
    ids = [int(i) for i in form.getlist("tx") if i.isdigit()][:300]
    back_to = form.get("back", "/transactions")
    if not back_to.startswith("/transactions"):
        back_to = "/transactions"
    uid = request.session["uid"]

    def work(ids):
        conn = get_conn()
        try:
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM transactions WHERE category_id IS NULL AND id IN ({','.join('?' * len(ids))})",
                ids)] if ids else []
            if not ids:
                return None
            suggestions, cost = ai.suggest_categories(conn, ids, uid)
            rows = {r["id"]: r for r in conn.execute(
                f"""SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id
                    WHERE t.id IN ({','.join('?' * len(ids))})""", ids)}
            return suggestions, cost, rows, grouped([dict(c) for c in all_categories(conn)])
        finally:
            conn.close()
    try:
        result = await run_in_threadpool(work, ids)
    except Exception as e:
        return render(request, "categorize_review.html", error=ai.friendly_error(e), items=[], cats=[],
                      rules=[], cost=0, back_to=back_to)
    if result is None:
        return back(back_to)
    suggestions, cost, rows, cats = result
    items = [dict(s, tx=rows[s["id"]]) for s in suggestions if s["id"] in rows]
    items.sort(key=lambda i: (i["category_id"] == 0, i["tx"]["description"]))
    rules = {}
    for i in items:
        if i["rule"].strip() and i["category_id"]:
            rules.setdefault((i["rule"].strip(), i["category_id"]), 0)
            rules[(i["rule"].strip(), i["category_id"])] += 1
    names = {c["id"]: c["name"] for _, group in cats for c in group}
    rule_list = sorted(({"pattern": p, "category_id": cid, "category": names.get(cid, "?"), "n": n}
                        for (p, cid), n in rules.items()), key=lambda r: -r["n"])
    return render(request, "categorize_review.html", error=None, items=items, cats=cats, rules=rule_list,
                  cost=cost, back_to=back_to)


@app.post("/ai/categorize/apply")
async def ai_categorize_apply(request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        changed = 0
        for key, value in form.items():
            if key.startswith("cat_") and key[4:].isdigit() and value.isdigit() and form.get(f"use_{key[4:]}"):
                conn.execute("UPDATE transactions SET category_id = ? WHERE id = ? AND category_id IS NULL",
                             (int(value), int(key[4:])))
                changed += 1
        for value in form.getlist("rule"):
            pattern, _, cid = value.rpartition("|")
            if pattern and cid.isdigit():
                conn.execute("INSERT INTO rules (pattern, category_id) VALUES (?, ?)", (pattern, int(cid)))
        conn.commit()
        ledger.apply_rules_to_uncategorized(conn)
    finally:
        conn.close()
    back_to = form.get("back", "/transactions")
    return back(back_to if back_to.startswith("/transactions") else "/transactions")


# ---------------------------------------------------------------- settings

@app.get("/settings")
def settings_page(request: Request, msg: str = ""):
    conn = get_conn()
    try:
        users = conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
        debt_plan = snowball.plan(conn)
        ai_month, ai_total = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN created_at >= ? THEN cost_cents END), 0), COALESCE(SUM(cost_cents), 0)
               FROM ai_usage""", (date.today().strftime("%Y-%m-01"),)).fetchone()
    finally:
        conn.close()
    phone = None
    if local.ENABLED:
        ip = local.lan_address()
        url = f"http://{ip}:{request.url.port or 80}" if ip else ""
        phone = {"on": local.sharing_on(), "url": url, "qr": Markup(local.qr_svg(url)) if url else ""}
    updates = {"auto": local.setting(updater.SETTING, "1") == "1"} if local.ENABLED else None
    return render(request, "settings.html", users=users, msg=msg, ai_month=ai_month, ai_total=ai_total,
                  p=debt_plan, phone=phone, updates=updates)


@app.post("/settings/phone")
async def phone_access(request: Request):
    form = await request.form()
    on = form.get("on") == "1"
    local.set_sharing(on)
    return back("/settings?msg=" + ("Phone+access+is+on." if on else "Phone+access+is+off.") + "#phone")


@app.post("/settings/anthropic")
async def anthropic_key(request: Request):
    form = await request.form()
    if form.get("action") == "remove":
        try:
            os.remove(ai.config_path())
        except OSError:
            pass
        return back("/settings?msg=Claude+API+key+removed.")
    key = form.get("api_key", "").strip()
    if not key:
        return back("/settings?msg=Paste+an+API+key+first.")
    try:
        await run_in_threadpool(ai.check_key, key)
    except ai.AIError as e:
        return back("/settings?" + urlencode({"msg": str(e)}))
    ai.save_key(key)
    return back("/settings?msg=Claude+is+set+up.+Try+Ask+Claude+in+the+menu.")


@app.post("/settings/users")
async def user_add(request: Request):
    form = await request.form()
    username, password = form.get("username", "").strip(), form.get("password", "")
    if not username or len(password) < 8:
        return back("/settings?msg=Usernames+need+a+password+of+at+least+8+characters.")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     (username, auth.hash_password(password)))
        conn.commit()
    except sqlite3.IntegrityError:
        return back("/settings?msg=That+username+is+taken.")
    finally:
        conn.close()
    return back(f"/settings?msg=Added+{username}.")


@app.post("/settings/password")
async def password_change(request: Request):
    form = await request.form()
    current, new = form.get("current", ""), form.get("new", "")
    conn = get_conn()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (request.session["uid"],)).fetchone()
        if not user or not auth.verify_password(current, user["password_hash"]):
            return back("/settings?msg=Current+password+is+wrong.")
        if len(new) < 8:
            return back("/settings?msg=New+password+needs+at+least+8+characters.")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(new), user["id"]))
        conn.commit()
    finally:
        conn.close()
    return back("/settings?msg=Password+changed.")


@app.post("/settings/users/{user_id}/delete")
def user_delete(user_id: int, request: Request):
    if user_id == request.session.get("uid"):
        return back("/settings?msg=You+can't+remove+yourself.")
    conn = get_conn()
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return back("/settings?msg=User+removed.")


@app.get("/backup")
def backup():
    path = os.path.join(db.DATA_DIR, "download-backup.db")
    src, dst = db.connect(), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return FileResponse(path, filename=f"home-finance-{date.today().isoformat()}.db",
                        media_type="application/octet-stream")


@app.get("/export.csv")
def export_csv():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT t.date, a.name, t.description, t.amount_cents, c.name, t.notes FROM transactions t
               JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id
               ORDER BY t.date, t.id"""
        ).fetchall()
    finally:
        conn.close()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Account", "Description", "Amount", "Category", "Notes"])
    for d, acct, desc, cents, cat, notes in rows:
        writer.writerow([d, acct, desc, f"{cents / 100:.2f}", cat or "", notes])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="transactions-{date.today().isoformat()}.csv"'})


# ---------------------------------------------------------------- updates (desktop app)

@app.get("/update")
async def update_page(request: Request):
    if not updater.wait_for_check(0):
        await run_in_threadpool(updater.check)
    st = updater.state
    notes = Markup(md.markdown(html.escape(st["notes"], quote=False), extensions=["tables"])) if st["notes"] else ""
    return render(request, "update.html", st=st, available=updater.available(), notes=notes,
                  reason=updater.why_not_automatic() if updater.available() else "",
                  size_mb=round((st["asset"] or {}).get("size", 0) / 1048576))


@app.post("/update/check")
async def update_check():
    await run_in_threadpool(updater.check)
    return back("/update")


@app.get("/update/dismiss")
def update_dismiss(request: Request, next: str = "/"):
    request.session["update_dismissed"] = updater.state["latest"]
    return back(next if next.startswith("/") and not next.startswith("//") else "/")


@app.post("/api/update/start")
def update_start(request: Request):
    if request.headers.get("x-home-finance") != "1":  # forces a CORS preflight, so other sites can't trigger it
        return JSONResponse({"error": "missing header"}, status_code=400)
    error = updater.start()
    return JSONResponse({"error": error} if error else {"ok": True})


@app.get("/api/update/status")
def update_status():
    st = updater.state
    return {"phase": st["phase"], "progress": st["progress"], "error": st["error"], "latest": st["latest"]}


@app.post("/settings/updates")
async def update_settings(request: Request):
    form = await request.form()
    on = form.get("auto") == "1"
    local.save_setting(updater.SETTING, "1" if on else "0")
    return back("/settings?msg=" + ("Home+Finance+will+check+for+updates+when+it+starts." if on
                                    else "Automatic+update+checks+are+off.") + "#updates")
