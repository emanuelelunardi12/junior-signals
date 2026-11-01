"""Handles updates Telegram sends to our webhook: commands and button taps."""
import re

from app import behaviour, config, db, telegram

SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")

HELP = (
    "<b>Commands</b>\n"
    "/add TICKER  add a ticker to your watchlist\n"
    "/remove TICKER  remove it\n"
    "/list  show your watchlist\n"
    "/threshold N  only alert me for scores of N or more (default 8)\n"
    "/pause  and  /resume  stop or restart alerts\n"
    "/privacy  what we record and how to switch it off\n"
    "/stop  delete my account and data\n\n"
    f"Dashboard: {config.BASE_URL}/dashboard"
)


def welcome_text(first_name: str | None) -> str:
    name = f", {first_name}" if first_name else ""
    return (f"You're connected{name}. Alerts for the tickers on your watchlist will arrive here.\n\n"
            f"Add tickers on the dashboard or with /add TICKER.\n\n{HELP}\n\n<i>{telegram.DISCLAIMER}</i>")


def _reply(chat_id: int, text: str) -> None:
    try:
        telegram.send_message(chat_id, text)
    except telegram.TelegramError as exc:
        print(f"[bot] reply failed: {exc}")


def handle_command(user: dict, chat_id: int, text: str) -> None:
    uid = user["id"]
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        db.upsert_user(uid, user.get("first_name"), user.get("username"))
        db.set_user(uid, can_message=1)
        _reply(chat_id, welcome_text(user.get("first_name")))
        return

    if db.get_user(uid) is None:
        _reply(chat_id, "Send /start first to connect your account.")
        return

    sym = arg.upper() if SYMBOL.match(arg.upper()) else ""
    if cmd in ("/add", "/remove") and arg and not sym:
        _reply(chat_id, "That doesn't look like a ticker symbol, e.g. /add EXMP.V")
    elif cmd == "/add" and sym:
        ok = db.add_to_watchlist(uid, sym)
        _reply(chat_id, f"Added {sym}." if ok else
               f"We don't track {sym} yet. Request noted; we'll tell you when it's added.")
        if ok:
            _track_watchlist(uid, "add", sym)
    elif cmd == "/remove" and sym:
        db.remove_from_watchlist(uid, sym)
        _reply(chat_id, f"Removed {sym}.")
        _track_watchlist(uid, "remove", sym)
    elif cmd == "/list":
        wl = db.watchlist(uid)
        _reply(chat_id, "Your watchlist: " + ", ".join(wl) if wl else "Your watchlist is empty. Use /add TICKER.")
    elif cmd == "/threshold" and arg.isdigit():
        n = max(1, min(int(arg), 20))
        db.set_user(uid, alert_threshold=n)
        _reply(chat_id, f"You'll get alerts for scores of {n} or more.")
    elif cmd == "/privacy":
        _reply(chat_id, PRIVACY)
    elif cmd == "/tracking" and arg.lower() in ("on", "off"):
        db.set_user(uid, tracking_opt_out=1 if arg.lower() == "off" else 0)
        _reply(chat_id, "Behaviour recording is off for you." if arg.lower() == "off" else "Behaviour recording is on.")
    elif cmd == "/pause":
        db.set_user(uid, paused=1)
        _reply(chat_id, "Alerts paused. Send /resume to restart.")
    elif cmd == "/resume":
        db.set_user(uid, paused=0)
        _reply(chat_id, "Alerts resumed.")
    elif cmd == "/stop":
        behaviour.forget_telegram_user(uid)
        db.delete_user(uid)
        _reply(chat_id, "Your account and data are deleted. Send /start any time to come back.")
    else:
        _reply(chat_id, HELP)


PRIVACY = (
    "<b>What we record</b>\n"
    "Your watchlist and alert settings, the alerts we send you, and your Useful / Not useful taps.\n"
    "On the dashboard we also record reading behaviour (which signals you open, for how long, and source clicks) "
    "under an anonymous session id, never your Telegram id, to learn which kinds of signals are worth scoring higher. "
    "A founder approves every change that learning proposes.\n\n"
    "/tracking off  stop recording reading behaviour\n/stop  delete your account and data\n"
    f"Details: {config.BASE_URL}/privacy"
)


def _track_watchlist(uid: int, action: str, symbol: str) -> None:
    if behaviour.tracking_allowed(db.get_user(uid)):
        behaviour.record_watchlist(behaviour.telegram_session_id(uid), "telegram", action, ticker=symbol)


def handle_callback(cq: dict) -> None:
    data = str(cq.get("data", ""))
    uid = (cq.get("from") or {}).get("id")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "fb" or not parts[1].isdigit() or parts[2] not in ("0", "1") or not uid:
        return  # malformed or foreign callback data: ignore quietly
    signal_id, useful = int(parts[1]), int(parts[2])
    if db.get().execute("SELECT 1 FROM signals WHERE id=? AND status='published'", (signal_id,)).fetchone() is None:
        return
    user = db.get_user(uid)
    if user is None:
        return
    state = behaviour.set_rating(uid, signal_id, useful, toggle=False)  # Telegram buttons show no state
    if behaviour.tracking_allowed(user):
        behaviour.record_rating(behaviour.telegram_session_id(uid), "telegram", signal_id,
                                "like" if state == 1 else "dislike")
    try:
        telegram.answer_callback(cq["id"], "Thanks, noted.")
    except (telegram.TelegramError, KeyError):
        pass


def handle_update(update: dict) -> None:
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return
    msg = update.get("message")
    if not msg or "from" not in msg:
        return
    if msg.get("chat", {}).get("type") != "private":
        return  # the bot only works in private chats
    text = msg.get("text", "")
    if text.startswith("/"):
        handle_command(msg["from"], msg["chat"]["id"], text)
    else:
        _reply(msg["chat"]["id"], HELP)
