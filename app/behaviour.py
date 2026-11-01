"""User-behaviour instrumentation: the fuel of the learning loop (app/learning.py).

What we record, all keyed by an ANONYMOUS session id, never by Telegram user id:
- impression    a published signal was on screen (at least half of it, for a second); position in the list
- open          the user expanded "Why this score"
- dwell         seconds the expanded signal stayed open on a visible page (capped at MAX_DWELL)
- scroll_past   the user scrolled past a signal they had seen without opening it
- source_click  the user clicked through to the primary source
- like / dislike / like_cleared   Useful / Not useful taps (recorded server-side, never via the events API)
- watchlist add / remove, and event-type filters used on the feed (watchlist_events)
Telegram deliveries (deliveries table) count as Telegram impressions.

Session ids: web = random token created at login and kept in the signed session cookie (the browser never
chooses it); Telegram = keyed hash of the user id and the UTC date, so it changes daily and can't be linked
back to a person without the server secret.

Opt-out: users.tracking_opt_out, or the browser's Global Privacy Control header, switches recording off.
Explicit Useful / Not useful ratings still count in per-event-type totals (they are stored in `feedback`
anyway so the buttons can show their state).

Free-text comments are side input only: stored for founders to read, never used by the learning loop or sent
to the AI.
"""
import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app import config, db

MAX_DWELL = 600              # seconds; longer usually means a forgotten tab
MAX_EVENTS_PER_BATCH = 50
MAX_COMMENT = 500
BROWSER_ACTIONS = ("impression", "open", "dwell", "scroll_past", "source_click")
RATING_ACTIONS = ("like", "dislike", "like_cleared")
_SID = re.compile(r"^[A-Za-z0-9_-]{16,40}$")


class BrowserEvent(BaseModel):
    """One event as the browser sends it. Anything else is rejected."""
    a: Literal["impression", "open", "dwell", "scroll_past", "source_click"]
    s: int = Field(gt=0, lt=2**31)
    v: float | None = Field(default=None, ge=0, le=86_400)
    p: int | None = Field(default=None, ge=0, le=1000)


class EventBatch(BaseModel):
    events: list[BrowserEvent] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


def parse_batch(raw) -> EventBatch | None:
    try:
        return EventBatch.model_validate(raw)
    except ValidationError:
        return None


# ---------------- anonymous session ids ----------------

def new_session_id() -> str:
    return secrets.token_urlsafe(16)


def valid_session_id(sid) -> bool:
    return isinstance(sid, str) and bool(_SID.match(sid))


def web_session_id(session: dict) -> str:
    """The anonymous id for this browser session; created server-side if missing or malformed."""
    sid = session.get("sid")
    if not valid_session_id(sid):
        sid = new_session_id()
        session["sid"] = sid
    return sid


def telegram_session_id(user_id: int, day: str | None = None) -> str:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hmac.new(config.SESSION_SECRET.encode(), f"tg:{user_id}:{day}".encode(), hashlib.sha256).hexdigest()
    return "tg-" + digest[:24]


def tracking_allowed(user, headers=None) -> bool:
    if user is None or user["tracking_opt_out"]:
        return False
    if headers is not None and headers.get("sec-gpc", "").strip() == "1":
        return False  # Global Privacy Control
    return True


# ---------------- recording ----------------

def _signal_meta(signal_ids) -> dict[int, tuple[str, str]]:
    ids = sorted(set(signal_ids))
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = db.get().execute(f"SELECT id, event_type, ticker FROM signals WHERE status='published' AND id IN ({q})", ids)
    return {r["id"]: (r["event_type"], r["ticker"]) for r in rows}


def record_browser_events(session_id: str, batch: EventBatch) -> int:
    """Store validated browser events for live signals. Unknown or unpublished signals are dropped."""
    meta = _signal_meta(e.s for e in batch.events)
    rows = []
    now = db.now()
    for e in batch.events:
        if e.s not in meta:
            continue
        event_type, ticker = meta[e.s]
        value = min(e.v, MAX_DWELL) if (e.a == "dwell" and e.v is not None) else None
        if e.a == "dwell" and not value:
            continue
        position = e.p if e.a == "impression" else None
        rows.append((now, session_id, "web", e.a, e.s, event_type, ticker, value, position))
    if rows:
        with db.tx() as c:
            c.executemany("""INSERT INTO behaviour_events (created_at, session_id, surface, action, signal_id,
                             event_type, ticker, value, position) VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def record_rating(session_id: str, surface: str, signal_id: int, action: str) -> None:
    if action not in RATING_ACTIONS:
        return
    meta = _signal_meta([signal_id])
    if signal_id not in meta:
        return
    event_type, ticker = meta[signal_id]
    with db.tx() as c:
        c.execute("""INSERT INTO behaviour_events (created_at, session_id, surface, action, signal_id, event_type, ticker)
                     VALUES (?,?,?,?,?,?,?)""", (db.now(), session_id, surface, action, signal_id, event_type, ticker))


def record_watchlist(session_id: str, surface: str, action: str, ticker: str | None = None,
                     event_type: str | None = None) -> None:
    if action not in ("add", "remove", "filter"):
        return
    with db.tx() as c:
        c.execute("""INSERT INTO watchlist_events (created_at, session_id, surface, action, ticker, event_type)
                     VALUES (?,?,?,?,?,?)""", (db.now(), session_id, surface, action, ticker, event_type))


def set_rating(user_id: int, signal_id: int, useful: int, toggle: bool = True) -> int | None:
    """Store a Useful (1) / Not useful (0) tap. Tapping the active choice again clears it (toggle).
    Returns the new state: 1, 0 or None."""
    c = db.get()
    old = c.execute("SELECT useful FROM feedback WHERE signal_id=? AND user_id=?", (signal_id, user_id)).fetchone()
    with db.tx() as c:
        if old is not None and old["useful"] == useful and toggle:
            c.execute("DELETE FROM feedback WHERE signal_id=? AND user_id=?", (signal_id, user_id))
            return None
        c.execute("""INSERT INTO feedback (signal_id, user_id, useful, created_at) VALUES (?,?,?,?)
                     ON CONFLICT(signal_id, user_id) DO UPDATE SET useful=excluded.useful, created_at=excluded.created_at""",
                  (signal_id, user_id, useful, db.now()))
    return useful


def clean_comment(text: str) -> str:
    """Plain text only: no control characters, collapsed whitespace, bounded length. Escaped again on output."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text or "")
                   if ch in "\n" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_COMMENT]


def save_comment(session_id: str, signal_id: int | None, text: str) -> bool:
    text = clean_comment(text)
    if len(text) < 2:
        return False
    if signal_id is not None and not _signal_meta([signal_id]):
        return False
    with db.tx() as c:
        c.execute("INSERT INTO comments (created_at, session_id, signal_id, text) VALUES (?,?,?,?)",
                  (db.now(), session_id, signal_id, text))
    return True


def forget_telegram_user(user_id: int, days: int | None = None) -> int:
    """On /stop: delete Telegram-side events for this user (their daily ids can be recomputed with the secret)."""
    from datetime import timedelta
    days = days or config.BEHAVIOUR_RETENTION_DAYS
    today = datetime.now(timezone.utc).date()
    ids = [telegram_session_id(user_id, (today - timedelta(days=i)).isoformat()) for i in range(days + 1)]
    n = 0
    with db.tx() as c:
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            q = ",".join("?" * len(chunk))
            n += c.execute(f"DELETE FROM behaviour_events WHERE session_id IN ({q})", chunk).rowcount
            n += c.execute(f"DELETE FROM watchlist_events WHERE session_id IN ({q})", chunk).rowcount
    return n


def purge_older_than(days: int | None = None) -> int:
    """Data minimisation: raw events are kept for `days`; the learning loop keeps only aggregates beyond that."""
    from datetime import timedelta
    days = days or config.BEHAVIOUR_RETENTION_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.tx() as c:
        n = c.execute("DELETE FROM behaviour_events WHERE created_at < ?", (cutoff,)).rowcount
        n += c.execute("DELETE FROM watchlist_events WHERE created_at < ?", (cutoff,)).rowcount
        n += c.execute("DELETE FROM comments WHERE created_at < ?", (cutoff,)).rowcount
    return n


# ---------------- per-event-type engagement ----------------

def _window(column: str, since: str | None, until: str | None) -> tuple[str, list]:
    sql, args = "", []
    if since:
        sql += f" AND {column} >= ?"
        args.append(since)
    if until:
        sql += f" AND {column} < ?"
        args.append(until)
    return sql, args


def _ratio(num, den):
    return round(num / den, 4) if den else None


def engagement_by_event_type(since: str | None = None, until: str | None = None,
                             min_weights_version: int | None = None) -> dict[str, dict]:
    """Open rate, like rate, click-through, skip rate and average dwell per event type.

    Web counts use distinct (session, signal) pairs, so reloading a page does not inflate anything.
    Likes come from the current Useful / Not useful state in `feedback`, one per user and signal.
    min_weights_version: only signals scored with that weights version or later (before/after comparisons).
    """
    c = db.get()
    ver_sql, ver_args = ("", [])
    if min_weights_version is not None:
        ver_sql, ver_args = " AND s.weights_version >= ?", [min_weights_version]
    wsql, wargs = _window("b.created_at", since, until)
    rows = c.execute(f"""
        SELECT b.event_type, b.action, COUNT(DISTINCT b.session_id || ':' || b.signal_id) AS pairs
        FROM behaviour_events b JOIN signals s ON s.id = b.signal_id
        WHERE b.surface='web' AND b.action IN ('impression','open','scroll_past','source_click') {wsql} {ver_sql}
        GROUP BY b.event_type, b.action""", wargs + ver_args).fetchall()
    out: dict[str, dict] = {}

    def slot(et):
        return out.setdefault(et, {"impressions": 0, "opens": 0, "scroll_pasts": 0, "source_clicks": 0,
                                   "telegram_deliveries": 0, "likes": 0, "dislikes": 0, "avg_dwell": None})
    key = {"impression": "impressions", "open": "opens", "scroll_past": "scroll_pasts", "source_click": "source_clicks"}
    for r in rows:
        slot(r["event_type"])[key[r["action"]]] = r["pairs"]

    dwell = c.execute(f"""
        SELECT event_type, AVG(total) AS avg_dwell FROM (
            SELECT b.event_type, b.session_id, b.signal_id, MIN(SUM(b.value), {MAX_DWELL}) AS total
            FROM behaviour_events b JOIN signals s ON s.id = b.signal_id
            WHERE b.action='dwell' {wsql} {ver_sql} GROUP BY b.event_type, b.session_id, b.signal_id)
        GROUP BY event_type""", wargs + ver_args).fetchall()
    for r in dwell:
        slot(r["event_type"])["avg_dwell"] = round(r["avg_dwell"], 1)

    dsql, dargs = _window("x.sent_at", since, until)
    for r in c.execute(f"""SELECT s.event_type, COUNT(*) n FROM deliveries x JOIN signals s ON s.id = x.signal_id
                          WHERE 1=1 {dsql} {ver_sql} GROUP BY s.event_type""", dargs + ver_args):
        slot(r["event_type"])["telegram_deliveries"] = r["n"]

    fsql, fargs = _window("f.created_at", since, until)
    for r in c.execute(f"""SELECT s.event_type, SUM(f.useful) up, COUNT(*) n FROM feedback f
                          JOIN signals s ON s.id = f.signal_id WHERE 1=1 {fsql} {ver_sql}
                          GROUP BY s.event_type""", fargs + ver_args):
        slot(r["event_type"])["likes"] = r["up"] or 0
        slot(r["event_type"])["dislikes"] = r["n"] - (r["up"] or 0)

    for m in out.values():
        m["open_rate"] = _ratio(m["opens"], m["impressions"])
        m["ctr"] = _ratio(m["source_clicks"], m["impressions"])
        m["skip_rate"] = _ratio(m["scroll_pasts"], m["impressions"])
        m["ratings"] = m["likes"] + m["dislikes"]
        m["like_rate"] = _ratio(m["likes"], m["ratings"])
    return out
