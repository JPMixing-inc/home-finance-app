"""SQLite storage. All money is stored as integer cents."""
from __future__ import annotations

import os
import sqlite3

DATA_DIR = os.environ.get("FINANCE_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "finance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'checking',
    csv_mapping TEXT,
    balance_cents INTEGER,
    balance_at TEXT,
    color TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    group_name TEXT NOT NULL DEFAULT 'Other',
    kind TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense', 'income', 'transfer')),
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (category_id, month)
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    added INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'file',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE,
    fingerprint TEXT,
    pair_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS transactions_category ON transactions(category_id);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plaid_items (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    access_token TEXT NOT NULL,
    env TEXT NOT NULL,
    institution TEXT NOT NULL DEFAULT '',
    institution_id TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    logo TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'ok', 'login_required', 'error')),
    error TEXT NOT NULL DEFAULT '',
    last_sync TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plaid_accounts (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
    plaid_account_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    mask TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    subtype TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS ai_chats (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ai_summaries (
    month TEXT PRIMARY KEY,
    html TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY,
    feature TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    cost_cents REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY,
    account_id INTEGER UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    balance_cents INTEGER,
    apr REAL NOT NULL DEFAULT 0,
    min_payment_cents INTEGER NOT NULL DEFAULT 0,
    start_balance_cents INTEGER NOT NULL DEFAULT 0,
    start_date TEXT NOT NULL,
    included INTEGER NOT NULL DEFAULT 1,
    payment_match TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS net_worth_history (
    date TEXT PRIMARY KEY,
    assets_cents INTEGER NOT NULL,
    debts_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS debt_history (
    debt_id INTEGER NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    PRIMARY KEY (debt_id, date)
);
"""

# Columns added after the first release: (table, column, declaration)
MIGRATIONS = [
    ("accounts", "balance_cents", "INTEGER"),
    ("accounts", "balance_at", "TEXT"),
    ("imports", "source", "TEXT NOT NULL DEFAULT 'file'"),
    ("transactions", "pair_id", "INTEGER"),  # the other side of a transfer or card payment
    ("debts", "payment_match", "TEXT NOT NULL DEFAULT ''"),
    ("debts", "group_name", "TEXT NOT NULL DEFAULT ''"),  # e.g. MOHELA: loans imported together
    ("debts", "details", "TEXT NOT NULL DEFAULT ''"),  # JSON from a loan statement: status, due date...
    ("accounts", "color", "TEXT"),  # chosen brand color; otherwise the bank's
    ("plaid_items", "institution_id", "TEXT NOT NULL DEFAULT ''"),
    ("plaid_items", "color", "TEXT NOT NULL DEFAULT ''"),  # bank brand color from Plaid
    ("plaid_items", "logo", "TEXT NOT NULL DEFAULT ''"),  # base64 PNG from Plaid
    ("debts", "lender", "TEXT NOT NULL DEFAULT ''"),  # company, for debts typed in by hand
]

# (group, name, kind) in display order
DEFAULT_CATEGORIES = [
    ("Income", "Paycheck", "income"),
    ("Income", "Other Income", "income"),
    ("Housing", "Mortgage / Rent", "expense"),
    ("Housing", "Utilities", "expense"),
    ("Housing", "Internet & Phone", "expense"),
    ("Housing", "Home Maintenance", "expense"),
    ("Food", "Groceries", "expense"),
    ("Food", "Restaurants", "expense"),
    ("Transportation", "Gas", "expense"),
    ("Transportation", "Car Payment", "expense"),
    ("Transportation", "Car Maintenance", "expense"),
    ("Living", "Shopping", "expense"),
    ("Living", "Health & Medical", "expense"),
    ("Living", "Personal Care", "expense"),
    ("Living", "Entertainment", "expense"),
    ("Living", "Subscriptions", "expense"),
    ("Living", "Gifts & Donations", "expense"),
    ("Living", "Travel", "expense"),
    ("Financial", "Insurance", "expense"),
    ("Financial", "Debt Payments", "expense"),
    ("Financial", "Savings", "expense"),
    ("Other", "Miscellaneous", "expense"),
    ("Transfers", "Transfer", "transfer"),
    ("Transfers", "Credit Card Payment", "transfer"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> None:
    os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO categories (group_name, name, kind, position) VALUES (?, ?, ?, ?)",
                [(g, n, k, i) for i, (g, n, k) in enumerate(DEFAULT_CATEGORIES)],
            )
        conn.commit()
    finally:
        conn.close()


def user_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
