"""SQLite storage. Plain sqlite3 on purpose: easy to inspect, easy to swap for Postgres later."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import yaml

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,          -- Telegram user id (= private chat id)
    first_name      TEXT,
    username        TEXT,
    can_message     INTEGER NOT NULL DEFAULT 0,   -- 1 once the bot has successfully messaged them
    alert_threshold INTEGER NOT NULL DEFAULT 8,
    paused          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    tracking_opt_out INTEGER NOT NULL DEFAULT 0   -- 1 = record no reading behaviour for this user
);
CREATE TABLE IF NOT EXISTS tickers (
    symbol          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    exchange        TEXT,
    commodity       TEXT,
    cik             TEXT,
    market_cap_musd REAL,
    rss             TEXT NOT NULL DEFAULT '[]',   -- JSON list
    match           TEXT NOT NULL DEFAULT '[]',   -- JSON list
    price_symbol    TEXT                          -- symbol used by the price source (see app/prices.py)
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol  TEXT NOT NULL REFERENCES tickers(symbol),
    PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS ticker_requests (
    user_id    INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL REFERENCES tickers(symbol),
    source       TEXT NOT NULL,                  -- rss | edgar | manual
    url          TEXT NOT NULL UNIQUE,
    title        TEXT,
    published_at TEXT,
    text         TEXT,
    fetched_at   TEXT NOT NULL,
    event_type   TEXT,                           -- set by classification
    status       TEXT NOT NULL DEFAULT 'new'     -- new | noise | extracted | failed
);
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    ticker       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    headline     TEXT NOT NULL,
    fields       TEXT NOT NULL,                  -- JSON: verified fields with quotes
    rejected     TEXT NOT NULL DEFAULT '[]',     -- JSON: fields the grader rejected
    score        INTEGER NOT NULL,
    breakdown    TEXT NOT NULL,                  -- JSON: [[label, points], ...]
    tier         TEXT NOT NULL,                  -- alert | feed | archive
    status       TEXT NOT NULL,                  -- queued | published | withheld | retracted | draft | rejected | retro
    dedupe_key   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reviewed_by  INTEGER,
    published_at TEXT,
    delivery     TEXT NOT NULL DEFAULT 'watchers',  -- watchers | feed_only | broadcast
    status_note  TEXT                            -- why a signal was withheld or retracted
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker, created_at);
CREATE TABLE IF NOT EXISTS feedback (
    signal_id  INTEGER NOT NULL REFERENCES signals(id),
    user_id    INTEGER NOT NULL,
    useful     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (signal_id, user_id)
);
-- One row per Telegram message sent for a signal: never twice to the same user, and corrections reach
-- exactly the people who got the original.
CREATE TABLE IF NOT EXISTS deliveries (
    signal_id  INTEGER NOT NULL REFERENCES signals(id),
    user_id    INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (signal_id, user_id)
);
-- Implicit behaviour on published signals: the fuel of the learning loop (app/behaviour.py).
-- No user id is stored, only an anonymous session id. event_type and ticker are copied from the signal on the
-- server, never taken from the browser.
CREATE TABLE IF NOT EXISTS behaviour_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    session_id  TEXT NOT NULL,                   -- random per web login; daily keyed hash for Telegram taps
    surface     TEXT NOT NULL,                   -- web | telegram
    action      TEXT NOT NULL,                   -- impression | open | dwell | scroll_past | source_click | like | dislike | like_cleared
    signal_id   INTEGER NOT NULL REFERENCES signals(id),
    event_type  TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    value       REAL,                            -- dwell: seconds the opened signal was on screen (capped)
    position    INTEGER                          -- impression: place in the list, 0 = top
);
CREATE INDEX IF NOT EXISTS idx_behaviour_type ON behaviour_events(event_type, action, created_at);
CREATE INDEX IF NOT EXISTS idx_behaviour_signal ON behaviour_events(signal_id, action);
-- Watchlist actions and feed filters, same anonymity rules.
CREATE TABLE IF NOT EXISTS watchlist_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    surface     TEXT NOT NULL,                   -- web | telegram
    action      TEXT NOT NULL,                   -- add | remove | filter
    ticker      TEXT,                            -- add / remove
    event_type  TEXT                             -- filter: the event type the user narrowed the feed to
);
-- Optional free-text comments on a signal. Side input only: never fed to the learning loop or to the AI.
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    signal_id   INTEGER REFERENCES signals(id),
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    sent_by    INTEGER NOT NULL,
    recipients INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage         TEXT NOT NULL,                 -- classify | extract | grade
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DATABASE_PATH
    if str(path) != ":memory:":
        config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: sqlite3.Connection | None = None


def get() -> sqlite3.Connection:
    """Shared connection for the app. Tests replace it with use()."""
    global _conn
    if _conn is None:
        _conn = connect()
        init(_conn)
    return _conn


def use(conn: sqlite3.Connection) -> None:
    global _conn
    _conn = conn


@contextmanager
def tx():
    conn = get()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


MIGRATIONS = [
    # (table, column, definition) added after the first version; applied to older databases
    ("signals", "delivery", "TEXT NOT NULL DEFAULT 'watchers'"),
    ("tickers", "price_symbol", "TEXT"),
    ("signals", "status_note", "TEXT"),
    ("users", "tracking_opt_out", "INTEGER NOT NULL DEFAULT 0"),
]

# Data changes for older databases. Each statement is idempotent.
DATA_MIGRATIONS = [
    # 1 Nov 2026: publishing became automatic. 'approved' meant "a person approved it"; it is now 'published'.
    "UPDATE signals SET status='published' WHERE status='approved'",
    # Alert-tier signals that were waiting for review under the old rule become founder drafts.
    "UPDATE signals SET status='draft' WHERE status='pending_review'",
]


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for statement in DATA_MIGRATIONS:
        conn.execute(statement)
    conn.commit()


def sync_universe(path=None) -> int:
    """Load config/universe.yaml into the tickers table (insert or update)."""
    data = yaml.safe_load(open(path or config.UNIVERSE_PATH)) or {}
    rows = data.get("tickers", [])
    with tx() as c:
        for t in rows:
            c.execute(
                """INSERT INTO tickers (symbol, name, exchange, commodity, cik, market_cap_musd, rss, match, price_symbol)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, exchange=excluded.exchange,
                   commodity=excluded.commodity, cik=excluded.cik, market_cap_musd=excluded.market_cap_musd,
                   rss=excluded.rss, match=excluded.match, price_symbol=excluded.price_symbol""",
                (t["symbol"].upper(), t["name"], t.get("exchange"), t.get("commodity"),
                 str(t["cik"]) if t.get("cik") else None, t.get("market_cap_musd"),
                 json.dumps(t.get("rss") or []), json.dumps(t.get("match") or []), t.get("price_symbol")),
            )
    return len(rows)


# ---------- small query helpers used across the app ----------

def upsert_user(user_id: int, first_name: str | None, username: str | None) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO users (id, first_name, username, created_at) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username""",
            (user_id, first_name, username, now()),
        )


def set_user(user_id: int, **fields) -> None:
    allowed = {"can_message", "alert_threshold", "paused", "tracking_opt_out"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    with tx() as c:
        c.execute(f"UPDATE users SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                  [fields[k] for k in cols] + [user_id])


def get_user(user_id: int):
    return get().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def delete_user(user_id: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM watchlist WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM feedback WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM deliveries WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def ticker_exists(symbol: str) -> bool:
    return get().execute("SELECT 1 FROM tickers WHERE symbol=?", (symbol.upper(),)).fetchone() is not None


def add_to_watchlist(user_id: int, symbol: str) -> bool:
    """Returns True if added; False if the ticker is not tracked (then it is logged as a request)."""
    symbol = symbol.upper()
    with tx() as c:
        if not c.execute("SELECT 1 FROM tickers WHERE symbol=?", (symbol,)).fetchone():
            c.execute("INSERT INTO ticker_requests VALUES (?,?,?)", (user_id, symbol, now()))
            return False
        c.execute("INSERT OR IGNORE INTO watchlist VALUES (?,?)", (user_id, symbol))
    return True


def remove_from_watchlist(user_id: int, symbol: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user_id, symbol.upper()))


def watchlist(user_id: int) -> list[str]:
    rows = get().execute("SELECT symbol FROM watchlist WHERE user_id=? ORDER BY symbol", (user_id,))
    return [r["symbol"] for r in rows]


def log_usage(stage: str, model: str, input_tokens: int, output_tokens: int) -> None:
    with tx() as c:
        c.execute("INSERT INTO ai_usage (stage, model, input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?)",
                  (stage, model, input_tokens, output_tokens, now()))
