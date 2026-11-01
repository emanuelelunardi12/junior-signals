"""Minimal Telegram Bot API client (plain HTTPS, no framework) and message formatting."""
import hashlib
import hmac
import html
import json
import time
from urllib.parse import urlparse

import httpx

from app import config

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


def call(method: str, **params) -> dict:
    if not config.TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    try:
        r = httpx.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=params, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramError(f"{method}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise TelegramError(f"{method}: unexpected response")
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data["result"]


def send_message(chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return call("sendMessage", **params)


def answer_callback(callback_id: str, text: str = "") -> None:
    call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def set_webhook(url: str) -> dict:
    return call("setWebhook", url=url, secret_token=config.TELEGRAM_WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"])


# ---------------- Login Widget verification ----------------

def verify_login(params: dict, max_age_seconds: int = 3600) -> dict | None:
    """Check the Telegram Login Widget signature. Returns the user data if valid, else None.

    Telegram signs the fields with HMAC-SHA256, key = SHA256(bot_token).
    Never create a session without this check: anyone can type these fields into a URL.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return None  # the key would be SHA256(""), which anyone can compute: no bot, no login
    data = {k: v for k, v in params.items() if k != "hash"}
    received = params.get("hash", "")
    if not received or "id" not in data or "auth_date" not in data:
        return None
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(config.TELEGRAM_BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(data["auth_date"]) > max_age_seconds:
        return None
    return data


# ---------------- formatting ----------------

DISCLAIMER = "Information only, not investment advice."


def lead_quote(fields: dict) -> str | None:
    """The verbatim quote shown with a signal: the one behind the headline, else the first one stored."""
    quotes = (fields or {}).get("_quotes") or {}
    return quotes.get("headline") or next(iter(quotes.values()), None)


def source_domain(url: str) -> str:
    return urlparse(url or "").netloc.removeprefix("www.")


def format_signal(sig, dashboard_url: str) -> str:
    """sig is a signals row joined with documents (url). Carries the source link and a verbatim quote."""
    e = html.escape
    breakdown = json.loads(sig["breakdown"])
    why = "\n".join(f"  {'+' if p >= 0 else ''}{p}  {e(label)}" for label, p in breakdown)
    quote = lead_quote(json.loads(sig["fields"]))
    quote_line = f"\u201c{e(quote)}\u201d\n\n" if quote else ""
    return (
        f"<b>{e(sig['ticker'])}</b>  score {sig['score']}\n"
        f"{e(sig['headline'])}\n\n"
        f"{quote_line}"
        f"<b>Why this score</b>\n<pre>{why}</pre>\n"
        f'<a href="{e(sig["url"])}">Source ({e(source_domain(sig["url"]))})</a>  |  '
        f'<a href="{e(dashboard_url)}">Dashboard</a>\n'
        f"<i>{DISCLAIMER}</i>"
    )


def feedback_buttons(signal_id: int) -> list[list[dict]]:
    return [[{"text": "Useful", "callback_data": f"fb:{signal_id}:1"},
             {"text": "Not useful", "callback_data": f"fb:{signal_id}:0"}]]
