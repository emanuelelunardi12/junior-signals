"""Publishing. The alert path is AUTOMATIC BY DESIGN: there is no human approval step here.

    pipeline.process_document -> go_live(signal_id) -> red-line checks -> published -> Telegram + dashboard

Our edge is speed: a signal goes live as soon as it is scored. What keeps that safe is not a person in the
loop but two red lines checked in code on every signal (app/guardrails.py): a primary-source link plus a
verbatim quote, and no buy/sell advice. A signal that fails is withheld automatically, never queued for a person.

Founders keep two controls that sit OUTSIDE the alert path:
- signals they create by hand in /admin open as drafts, so the author can check the AI's extraction;
- a published signal can be retracted afterwards; everyone who received it gets a correction.
The human review that matters for safety is on changes to the scoring weights (app/learning.py, /admin/tuning).

Delivery modes:
- watchers:  users who watch the ticker and whose alert threshold is <= the score (default)
- feed_only: nobody is messaged; the signal only appears on the dashboard
- broadcast: every user who can be messaged and hasn't paused alerts
"""
import html
import json

from app import config, db, guardrails, telegram

DELIVERIES = {"watchers", "feed_only", "broadcast"}
LIVE = "published"


def signal_with_source(signal_id: int):
    return db.get().execute(
        """SELECT s.*, d.url, d.title AS doc_title, d.text AS doc_text
           FROM signals s JOIN documents d ON d.id = s.document_id WHERE s.id=?""",
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


def _send(uid: int, text: str, buttons=None) -> bool:
    try:
        telegram.send_message(uid, text, buttons)
        return True
    except telegram.TelegramError as exc:
        print(f"[publish] could not message {uid}: {exc}")
        if "blocked" in str(exc).lower() or "deactivated" in str(exc).lower():
            db.set_user(uid, can_message=0)
        return False


def _send_all(user_ids, text, buttons=None) -> int:
    return sum(_send(uid, text, buttons) for uid in user_ids)


def red_line_violations(sig) -> list[str]:
    return guardrails.violations(sig["headline"], json.loads(sig["fields"]), sig["url"], sig["doc_text"])


def go_live(signal_id: int) -> int:
    """Publish a signal now, with no human step: red-line checks, then dashboard + Telegram.

    Returns the number of users messaged. A signal that breaks a red line is marked 'withheld' with the
    reason and is neither shown nor sent. Safe to call twice: it never sends a signal to the same user twice.
    """
    sig = signal_with_source(signal_id)
    if sig is None or sig["status"] not in ("queued", "draft", LIVE):
        return 0
    if sig["status"] != LIVE:
        problems = red_line_violations(sig)
        with db.tx() as c:
            if problems:
                c.execute("UPDATE signals SET status='withheld', status_note=? WHERE id=?",
                          ("; ".join(problems), signal_id))
                print(f"[publish] signal {signal_id} withheld: {'; '.join(problems)}")
                return 0
            c.execute("UPDATE signals SET status=?, published_at=COALESCE(published_at, ?) WHERE id=?",
                      (LIVE, db.now(), signal_id))
    return deliver(signal_id)


def deliver(signal_id: int) -> int:
    """Telegram delivery for a live signal, according to its delivery mode. Never sends twice to a user."""
    sig = signal_with_source(signal_id)
    if sig is None or sig["status"] != LIVE:
        return 0
    if sig["delivery"] == "watchers" and sig["tier"] == "archive":
        return 0  # archive-tier signals are on the ticker page only
    already = {r["user_id"] for r in db.get().execute(
        "SELECT user_id FROM deliveries WHERE signal_id=?", (signal_id,))}
    text = telegram.format_signal(sig, f"{config.BASE_URL}/dashboard?t={sig['ticker']}")
    sent = 0
    for uid in recipients(sig["ticker"], sig["score"], sig["delivery"]):
        if uid in already:
            continue
        if _send(uid, text, telegram.feedback_buttons(signal_id)):
            sent += 1
            with db.tx() as c:
                c.execute("INSERT OR IGNORE INTO deliveries (signal_id, user_id, sent_at) VALUES (?,?,?)",
                          (signal_id, uid, db.now()))
    return sent


# Kept for callers and tests written before 1 Nov 2026.
publish_signal = go_live


def retract(signal_id: int, reason: str) -> int:
    """Take a live signal down after the fact and tell everyone who received it. Returns corrections sent."""
    sig = signal_with_source(signal_id)
    if sig is None or sig["status"] != LIVE:
        return 0
    with db.tx() as c:
        c.execute("UPDATE signals SET status='retracted', status_note=? WHERE id=?", (reason, signal_id))
    users = [r["user_id"] for r in db.get().execute("SELECT user_id FROM deliveries WHERE signal_id=?", (signal_id,))]
    text = (f"<b>Correction: {html.escape(sig['ticker'])}</b>\nWe withdrew this signal: "
            f"{html.escape(sig['headline'])}\nReason: {html.escape(reason)}\n<i>{telegram.DISCLAIMER}</i>")
    return _send_all(users, text)


def send_announcement(text: str, sent_by: int) -> int:
    """General message to every reachable user (not tied to a ticker). Use sparingly."""
    body = f"<b>Announcement</b>\n{html.escape(text)}"
    sent = _send_all(recipients("", 0, "broadcast"), body)
    with db.tx() as c:
        c.execute("INSERT INTO announcements (text, sent_by, recipients, created_at) VALUES (?,?,?,?)",
                  (text, sent_by, sent, db.now()))
    return sent
