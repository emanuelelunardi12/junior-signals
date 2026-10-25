"""Send approved signals to users.

Delivery modes (set by the reviewer in /admin; pipeline signals default to 'watchers'):
- watchers:  users who watch the ticker and whose alert threshold is <= the score
- feed_only: nobody is messaged; the signal only appears on the dashboard
- broadcast: every user who can be messaged and hasn't paused alerts
"""
import html

from app import config, db, telegram

DELIVERIES = {"watchers", "feed_only", "broadcast"}


def signal_with_source(signal_id: int):
    return db.get().execute(
        "SELECT s.*, d.url, d.title AS doc_title FROM signals s JOIN documents d ON d.id = s.document_id WHERE s.id=?",
        (signal_id,),
    ).fetchone()


def recipients(ticker: str, score: int, delivery: str = "watchers") -> list[int]:
    if delivery == "feed_only":
        return []
    if delivery == "broadcast":
        rows = db.get().execute("SELECT id FROM users WHERE can_message=1 AND paused=0").fetchall()
    else:
        rows = db.get().execute(
            """SELECT u.id FROM users u JOIN watchlist w ON w.user_id = u.id
               WHERE w.symbol=? AND u.can_message=1 AND u.paused=0 AND u.alert_threshold <= ?""",
            (ticker, score),
        ).fetchall()
    return [r["id"] for r in rows]


def _send_all(user_ids, text, buttons=None) -> int:
    sent = 0
    for uid in user_ids:
        try:
            telegram.send_message(uid, text, buttons)
            sent += 1
        except telegram.TelegramError as exc:
            print(f"[publish] could not message {uid}: {exc}")
            if "blocked" in str(exc).lower() or "deactivated" in str(exc).lower():
                db.set_user(uid, can_message=0)
    return sent


def publish_signal(signal_id: int) -> int:
    """Send one approved signal according to its delivery mode. Returns users messaged. Never sends twice."""
    sig = signal_with_source(signal_id)
    if sig is None or sig["status"] != "approved" or sig["published_at"]:
        return 0
    delivery = sig["delivery"]
    if delivery == "watchers" and sig["tier"] == "archive":
        return 0
    text = telegram.format_signal(sig, f"{config.BASE_URL}/dashboard?t={sig['ticker']}")
    sent = _send_all(recipients(sig["ticker"], sig["score"], delivery), text, telegram.feedback_buttons(signal_id))
    with db.tx() as c:
        c.execute("UPDATE signals SET published_at=? WHERE id=?", (db.now(), signal_id))
    return sent


def send_announcement(text: str, sent_by: int) -> int:
    """General message to every reachable user (not tied to a ticker). Use sparingly."""
    body = f"<b>Announcement</b>\n{html.escape(text)}"
    sent = _send_all(recipients("", 0, "broadcast"), body)
    with db.tx() as c:
        c.execute("INSERT INTO announcements (text, sent_by, recipients, created_at) VALUES (?,?,?,?)",
                  (text, sent_by, sent, db.now()))
    return sent
