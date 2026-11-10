"""Claude features: ask questions about the data, suggest categories, write monthly summaries.

The API key lives in data/anthropic.json (not the database). Claude reads the household data only
through query_database, which runs on a read-only connection that can't see logins or bank tokens.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import date
from typing import Optional

import anthropic
import markdown as md

from . import db

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]  # re-run a declined request on Anthropic's recommended model
MAX_TOOL_ROUNDS = 10
MAX_ROWS = 200

# claude-opus-5-5 prices in dollars per million tokens (cache writes are 1.25x input)
PRICE = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}

HIDDEN_TABLES = {"users", "plaid_items"}  # passwords and bank access tokens
SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)  # WITH RECURSIVE

SCHEMA_GUIDE = """\
SQLite database for one household's budget. All money is integer cents.

accounts(id, name, kind, balance_cents, balance_at)
  kind is checking, savings, credit or cash. balance_cents comes from bank sync (NULL if never
  synced); for credit cards it is negative, meaning what's owed.
categories(id, name, group_name, kind, position)
  kind is 'expense', 'income' or 'transfer'. Transfers (credit card payments, moving money between
  the household's own accounts) are neither spending nor income.
budgets(category_id, month, amount_cents)
  month is 'YYYY-MM'. One row per budgeted category per month; missing means no budget.
transactions(id, account_id, date, description, amount_cents, category_id, notes, import_id)
  date is 'YYYY-MM-DD'. amount_cents < 0 is money out, > 0 is money in. category_id NULL means
  uncategorized. On a credit card, purchases are negative and payments to the card are positive.
rules(id, pattern, category_id)
  Auto-categorize rules: a description containing pattern (case-insensitive) gets category_id.
imports(id, account_id, filename, added, skipped, source, created_at)
plaid_accounts(id, item_id, name, mask, type, subtype, account_id)
debts(id, account_id, name, balance_cents, apr, min_payment_cents, start_balance_cents, start_date, included)
  The household's debt snowball plan. For a debt with account_id, the current balance owed is
  -accounts.balance_cents (balance_cents here is only for hand-entered debts). min_payment_cents 0
  means unknown. included: 1 = in the snowball (prioritized, smallest balance first), 2 = counted in
  total debt but not prioritized (paid after the others), 0 = left out of the plan entirely.
debt_history(debt_id, date, balance_cents)  daily balance snapshots, positive = owed
settings(key, value)  'snowball_extra_cents' is the extra paid toward debt each month.

How the site counts things, so your numbers match its pages:
- Spending for a month = -SUM(amount_cents) over transactions in 'expense' categories, plus the money
  out of uncategorized transactions. Refunds in an expense category reduce its spending.
- Income for a month = SUM(amount_cents) in 'income' categories, plus money in that's uncategorized.
- Transactions in 'transfer' categories are left out of both.
- Filter a month with date >= 'YYYY-MM-01' AND date < the first day of the next month.
"""

CHAT_SYSTEM = f"""\
You are the assistant inside Home Finance, a budgeting site a married couple runs on their home \
server. They share all accounts. Their main goal right now is paying off debt with the debt \
snowball method: minimums on everything, every extra dollar to the smallest balance, then roll that \
payment into the next. When it's relevant, connect answers back to that goal (money that could go \
to the current target debt). Answer their questions about their own money using the \
query_database tool: always look the numbers up rather than guessing, and check your SQL results \
make sense before answering.

{SCHEMA_GUIDE}
Writing the answer:
- Lead with the direct answer. Keep it short: a sentence or two, then details only if they help.
- Format money like $1,234.56. Use a small markdown table when comparing several numbers.
- Say which period you looked at when it isn't obvious, and mention uncategorized spending if it
  could change the answer.
- You can only read data. If they ask you to change something (categorize, set a budget), tell them
  where on the site to do it: Transactions, Budget, Categories, Accounts, Import or Bank sync.
- This is general budgeting help, not professional financial, tax or legal advice; say so only
  when they ask for that kind of advice.
"""

SUMMARY_PROMPT = """\
Write a short summary of {label} for the dashboard. Use query_database to look at this month's \
spending against the budget, how it compares with the previous three months, the biggest changes, \
and anything worth a look: new recurring charges or subscriptions, unusually large or duplicate-looking \
charges, and uncategorized transactions. Start with their debt snowball: progress on the current target \
(the included debt with the smallest balance) and how this month's spending helps or hurts it. {partial}

Format: 3 to 6 markdown bullet points, each one sentence with specific numbers and merchant names. \
No heading, no intro sentence, no sign-off."""

CATEGORIZE_SYSTEM = """\
You categorize bank transactions for a household budget. For each transaction pick the best \
category_id from the list, or 0 if you can't tell with reasonable confidence (a person's name on a \
transfer, a vague description). Use transfer categories for credit card payments and moves between \
the household's own accounts, income categories only for money coming in. Also suggest a short \
rule pattern when the description has a stable merchant name that would identify future charges \
(for example 'KROGER' or 'NETFLIX'): a substring of the description, without store numbers, dates \
or reference codes. Leave rule empty when the merchant isn't clear."""


class AIError(Exception):
    pass


# ---------------------------------------------------------------- config

def config_path() -> str:
    return os.path.join(db.DATA_DIR, "anthropic.json")


def load_key() -> Optional[str]:
    try:
        with open(config_path()) as f:
            return json.load(f).get("api_key")
    except (OSError, ValueError):
        return None


def save_key(api_key: str) -> None:
    path = config_path()
    with open(path, "w") as f:
        json.dump({"api_key": api_key}, f)
    os.chmod(path, 0o600)


def client(api_key: Optional[str] = None) -> anthropic.Anthropic:
    api_key = api_key or load_key()
    if not api_key:
        raise AIError("Add an Anthropic API key under Settings first.")
    return anthropic.Anthropic(api_key=api_key, timeout=300.0)


def check_key(api_key: str) -> None:
    """Raise AIError unless the key works and can use the model."""
    try:
        client(api_key).models.retrieve(MODEL)
    except anthropic.AuthenticationError:
        raise AIError("Anthropic didn't accept that API key.") from None
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic returned an error: {e.message}") from None
    except anthropic.APIConnectionError:
        raise AIError("Couldn't reach Anthropic. Check the server's internet connection.") from None


def friendly_error(e: Exception) -> str:
    if isinstance(e, AIError):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic didn't accept the API key. Check it under Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude is busy or your Anthropic account hit a limit. Try again in a minute."
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code == 402 or "credit" in str(e.message).lower():
            return "Your Anthropic account is out of credit. Add some at console.anthropic.com."
        return f"Anthropic returned an error: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Check the server's internet connection."
    return f"Something went wrong: {e}"


# ---------------------------------------------------------------- cost tracking

def cost_cents(usage) -> float:
    dollars = (
        (usage.input_tokens or 0) * PRICE["input"]
        + (usage.output_tokens or 0) * PRICE["output"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * PRICE["cache_read"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * PRICE["cache_write"]
    ) / 1_000_000
    return dollars * 100


def log_usage(conn, feature: str, user_id: Optional[int], cents: float) -> None:
    conn.execute("INSERT INTO ai_usage (feature, user_id, cost_cents) VALUES (?, ?, ?)",
                 (feature, user_id, cents))


# ---------------------------------------------------------------- the read-only database tool

def _authorizer(action, arg1, arg2, _db, _trigger):
    if action == sqlite3.SQLITE_READ and arg1 in HIDDEN_TABLES:
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY  # no writes, attaches, pragmas or schema changes


def query_database(sql: str) -> str:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    try:
        conn.set_authorizer(_authorizer)
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    out = {"columns": columns, "rows": rows[:MAX_ROWS]}
    if len(rows) > MAX_ROWS:
        out["note"] = f"Only the first {MAX_ROWS} rows are shown; aggregate or add LIMIT."
    return json.dumps(out, default=str)


TOOLS = [{
    "name": "query_database",
    "description": (
        "Run one read-only SQLite SELECT against the household budget database and get the rows back "
        f"as JSON (at most {MAX_ROWS} rows). Use aggregates (SUM, GROUP BY) rather than pulling every "
        "row. Login and bank-token tables are not readable."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SQLite SELECT statement."}},
        "required": ["sql"],
        "additionalProperties": False,
    },
}]


def run_agent(conn, messages: list, system: str, effort: str, feature: str,
              user_id: Optional[int]) -> tuple[str, float]:
    """Tool loop. Appends every turn to `messages` (unchanged, so thinking blocks stay valid) and
    returns (the final answer's text, cost in cents)."""
    c = client()
    total = 0.0
    for _ in range(MAX_TOOL_ROUNDS):
        response = c.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=BETAS,
            fallbacks="default",
            system=system,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        cents = cost_cents(response.usage)
        total += cents
        log_usage(conn, feature, user_id, cents)
        conn.commit()
        messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})
        if response.stop_reason == "refusal":
            return "Claude declined to answer that one. Try asking it a different way.", total
        if response.stop_reason == "pause_turn":
            continue
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and tool_calls:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": query_database(b.input["sql"])}
                for b in tool_calls
            ]})
            continue
        text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n*(The answer was cut off because it ran long.)*"
        return text or "Claude didn't come back with an answer. Try rephrasing the question.", total
    return "That question needed more lookups than allowed. Try asking something narrower.", total


def to_html(text: str) -> str:
    # Escape first so nothing in a reply can inject markup; markdown syntax survives escaping.
    return md.markdown(html.escape(text, quote=False), extensions=["tables", "sane_lists"])


# ---------------------------------------------------------------- 1. chat

def ask(conn, chat_id: Optional[int], question: str, user_id: int) -> tuple[int, str, float]:
    """Answer a question in a saved conversation. Returns (chat id, answer html, cost in cents)."""
    row = None
    if chat_id:
        row = conn.execute("SELECT * FROM ai_chats WHERE id = ?", (chat_id,)).fetchone()
    messages = json.loads(row["messages"]) if row else []
    today = date.today()
    messages.append({"role": "user", "content": f"(Today is {today:%A, %B %-d, %Y}.)\n\n{question}"})
    answer, cost = run_agent(conn, messages, CHAT_SYSTEM, "medium", "chat", user_id)
    if row:
        conn.execute("UPDATE ai_chats SET messages = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (json.dumps(messages), row["id"]))
        chat_id = row["id"]
    else:
        chat_id = conn.execute("INSERT INTO ai_chats (user_id, title, messages) VALUES (?, ?, ?)",
                               (user_id, question[:80], json.dumps(messages))).lastrowid
    conn.commit()
    return chat_id, to_html(answer), cost


def chat_history(conn, chat_id: int) -> list[dict]:
    """The visible back-and-forth of a saved chat: [{'role': 'user'|'assistant', 'html': ...}]."""
    row = conn.execute("SELECT messages FROM ai_chats WHERE id = ?", (chat_id,)).fetchone()
    if not row:
        return []
    out = []
    for m in json.loads(row["messages"]):
        content = m["content"]
        if m["role"] == "user" and isinstance(content, str):
            question = content.split("\n\n", 1)[-1]
            out.append({"role": "user", "html": html.escape(question)})
        elif m["role"] == "assistant":
            text = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if text and not any(b.get("type") == "tool_use" for b in content):
                out.append({"role": "assistant", "html": to_html(text)})
    return out


# ---------------------------------------------------------------- 2. categorize

CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category_id": {"type": "integer"},
                    "rule": {"type": "string"},
                },
                "required": ["id", "category_id", "rule"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def suggest_categories(conn, tx_ids: list[int], user_id: int) -> tuple[list[dict], float]:
    """Ask Claude for a category (and maybe a rule) for each transaction. Returns (suggestions, cents)."""
    cats = conn.execute("SELECT id, name, group_name, kind FROM categories ORDER BY position").fetchall()
    valid = {c["id"] for c in cats}
    cat_lines = "\n".join(f"{c['id']}: {c['name']} ({c['group_name']}, {c['kind']})" for c in cats)
    c = client()
    suggestions, total = [], 0.0
    for start in range(0, len(tx_ids), 150):
        batch = tx_ids[start:start + 150]
        rows = conn.execute(
            f"""SELECT t.id, t.date, t.description, t.amount_cents, a.name AS account, a.kind
                FROM transactions t JOIN accounts a ON a.id = t.account_id
                WHERE t.id IN ({",".join("?" * len(batch))})""", batch).fetchall()
        tx_lines = "\n".join(
            f"{r['id']} | {r['date']} | {r['description']} | {r['amount_cents'] / 100:+.2f} | {r['account']} ({r['kind']})"
            for r in rows)
        response = c.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=BETAS,
            fallbacks="default",
            system=CATEGORIZE_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Categories (id: name (group, kind)):\n{cat_lines}\n\n"
                       f"Transactions (id | date | description | amount, negative = money out | account):\n{tx_lines}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": CATEGORIZE_SCHEMA}},
        )
        cents = cost_cents(response.usage)
        total += cents
        log_usage(conn, "categorize", user_id, cents)
        conn.commit()
        if response.stop_reason == "refusal":
            raise AIError("Claude declined to categorize these transactions.")
        if response.stop_reason == "max_tokens":
            raise AIError("Claude's answer was cut off. Try fewer transactions at once.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            raise AIError("Claude's answer couldn't be read. Try again.") from None
        wanted = set(batch)
        for s in data["suggestions"]:
            if s["id"] in wanted:
                s["category_id"] = s["category_id"] if s["category_id"] in valid else 0
                suggestions.append(s)
    return suggestions, total


# ---------------------------------------------------------------- 3. monthly summary

def summarize_month(conn, month: str, label: str, user_id: int) -> str:
    current = month == date.today().strftime("%Y-%m")
    partial = (f"The month isn't over: today is {date.today():%B %-d}, so compare pace rather than totals."
               if current else "")
    messages = [{"role": "user", "content": SUMMARY_PROMPT.format(label=label, partial=partial)}]
    text, _ = run_agent(conn, messages, CHAT_SYSTEM, "medium", "summary", user_id)
    body = to_html(text)
    conn.execute(
        """INSERT INTO ai_summaries (month, html, created_at) VALUES (?, ?, datetime('now', 'localtime'))
           ON CONFLICT (month) DO UPDATE SET html = excluded.html, created_at = excluded.created_at""",
        (month, body))
    conn.commit()
    return body
