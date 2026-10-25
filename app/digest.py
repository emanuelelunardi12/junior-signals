"""Weekly digest: one message per user summarising the week's signals on their watchlist."""
import html
from datetime import datetime, timedelta, timezone

from app import config, db, telegram


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def signals_for(user_id: int, days: int = 7):
    return db.get().execute(
        """SELECT s.*, d.url FROM signals s
           JOIN documents d ON d.id = s.document_id
           JOIN watchlist w ON w.symbol = s.ticker AND w.user_id = ?
           WHERE s.status='approved' AND s.tier!='archive' AND s.created_at >= ?
           ORDER BY s.score DESC, s.created_at DESC LIMIT 15""",
        (user_id, _since(days)),
    ).fetchall()


def format_digest(rows, days: int = 7) -> str:
    lines = [f"<b>Your week</b>  {len(rows)} signal{'s' if len(rows) != 1 else ''} in the last {days} days", ""]
    for s in rows:
        lines.append(f'{s["score"]}  <b>{html.escape(s["ticker"])}</b>  '
                     f'<a href="{html.escape(s["url"])}">{html.escape(s["headline"])}</a>')
    lines += ["", f'<a href="{config.BASE_URL}/dashboard">Open the dashboard</a>', f"<i>{telegram.DISCLAIMER}</i>"]
    return "\n".join(lines)


def send_weekly(days: int = 7, skip_empty: bool = True) -> int:
    """Send the digest to every reachable, unpaused user with a watchlist. Returns users messaged."""
    users = db.get().execute(
        """SELECT DISTINCT u.id FROM users u JOIN watchlist w ON w.user_id = u.id
           WHERE u.can_message=1 AND u.paused=0""").fetchall()
    sent = 0
    for u in users:
        rows = signals_for(u["id"], days)
        if not rows and skip_empty:
            continue
        try:
            telegram.send_message(u["id"], format_digest(rows, days))
            sent += 1
        except telegram.TelegramError as exc:
            print(f"[digest] could not message {u['id']}: {exc}")
    return sent
