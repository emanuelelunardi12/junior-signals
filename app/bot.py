"""Handles updates Telegram sends to our webhook: commands and button taps."""
from app import config, db, telegram

HELP = (
    "<b>Commands</b>\n"
    "/add TICKER  add a ticker to your watchlist\n"
    "/remove TICKER  remove it\n"
    "/list  show your watchlist\n"
    "/threshold N  only alert me for scores of N or more (default 8)\n"
    "/pause  and  /resume  stop or restart alerts\n"
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

    if cmd == "/add" and arg:
        ok = db.add_to_watchlist(uid, arg)
        _reply(chat_id, f"Added {arg.upper()}." if ok else
               f"We don't track {arg.upper()} yet. Request noted; we'll tell you when it's added.")
    elif cmd == "/remove" and arg:
        db.remove_from_watchlist(uid, arg)
        _reply(chat_id, f"Removed {arg.upper()}.")
    elif cmd == "/list":
        wl = db.watchlist(uid)
        _reply(chat_id, "Your watchlist: " + ", ".join(wl) if wl else "Your watchlist is empty. Use /add TICKER.")
    elif cmd == "/threshold" and arg.isdigit():
        db.set_user(uid, alert_threshold=int(arg))
        _reply(chat_id, f"You'll get alerts for scores of {int(arg)} or more.")
    elif cmd == "/pause":
        db.set_user(uid, paused=1)
        _reply(chat_id, "Alerts paused. Send /resume to restart.")
    elif cmd == "/resume":
        db.set_user(uid, paused=0)
        _reply(chat_id, "Alerts resumed.")
    elif cmd == "/stop":
        db.delete_user(uid)
        _reply(chat_id, "Your account and data are deleted. Send /start any time to come back.")
    else:
        _reply(chat_id, HELP)


def handle_callback(cq: dict) -> None:
    data = cq.get("data", "")
    uid = cq["from"]["id"]
    if data.startswith("fb:"):
        _, signal_id, useful = data.split(":")
        if db.get().execute("SELECT 1 FROM signals WHERE id=?", (int(signal_id),)).fetchone() is None:
            return
        if db.get_user(uid) is None:
            return
        with db.tx() as c:
            c.execute(
                """INSERT INTO feedback (signal_id, user_id, useful, created_at) VALUES (?,?,?,?)
                   ON CONFLICT(signal_id, user_id) DO UPDATE SET useful=excluded.useful, created_at=excluded.created_at""",
                (int(signal_id), uid, int(useful), db.now()),
            )
        try:
            telegram.answer_callback(cq["id"], "Thanks, noted.")
        except telegram.TelegramError:
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
