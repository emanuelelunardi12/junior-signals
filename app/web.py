"""Web dashboard + Telegram webhook. Run with: uvicorn app.web:app --reload"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import bot, config, db, publish, scoring, telegram

HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    print(config.describe())
    db.get()
    db.sync_universe()
    sched = None
    if config.RUN_SCHEDULER:
        from app import scheduler
        sched = scheduler.start()
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="Junior Signals", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 24 * 30,
                   https_only=config.BASE_URL.startswith("https"), same_site="lax")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["fromjson"] = json.loads

FIELD_LABELS = {
    "counterparty_name": "Investor", "counterparty_type": "Investor type", "amount_millions": "Amount",
    "currency": "Currency", "price_vs_market": "Price vs market", "full_warrants": "Full warrants",
    "stake_pct": "Stake", "board_seat": "Board seat", "topup_rights": "Top-up rights", "standstill": "Standstill",
    "role_title": "Role", "person_name": "Person", "event_date": "Date", "headline": "Headline",
}


def field_label(key: str) -> str:
    return FIELD_LABELS.get(key, key.replace("_", " ").capitalize())


def field_value(key: str, value) -> str:
    if isinstance(value, bool) or str(value).lower() in {"true", "false"}:
        return "yes" if str(value).lower() == "true" else "no"
    if key == "stake_pct":
        return f"{value:g}%" if isinstance(value, (int, float)) else f"{value}%"
    if key == "amount_millions":
        return f"{value:g} million" if isinstance(value, (int, float)) else f"{value} million"
    return str(value).replace("_", " ")


templates.env.filters["field_label"] = field_label
templates.env.globals["field_value"] = field_value


def current_user(request: Request):
    uid = request.session.get("uid")
    return db.get_user(uid) if uid else None


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS or (config.DEV_LOGIN and not config.ADMIN_IDS)


def _home():
    return RedirectResponse("/", status_code=303)


# ---------------- login ----------------

@app.get("/")
def index(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "bot_username": config.TELEGRAM_BOT_USERNAME,
        "auth_url": f"{config.BASE_URL}/auth/telegram",
        "dev_login": config.DEV_LOGIN,
        "error": request.query_params.get("error"),
    })


def _start_session(request: Request, uid: int, first_name: str | None, username: str | None):
    new = db.get_user(uid) is None
    db.upsert_user(uid, first_name, username)
    request.session["uid"] = uid
    user = db.get_user(uid)
    if new or not user["can_message"]:
        # Login asked for permission to message; the only reliable check is to try.
        try:
            telegram.send_message(uid, bot.welcome_text(first_name))
            db.set_user(uid, can_message=1)
        except telegram.TelegramError as exc:
            print(f"[login] welcome message failed for {uid}: {exc}")


@app.get("/auth/telegram")
def auth_telegram(request: Request):
    data = telegram.verify_login(dict(request.query_params))
    if data is None:
        return RedirectResponse("/?error=login", status_code=303)
    _start_session(request, int(data["id"]), data.get("first_name"), data.get("username"))
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/dev-login")
def dev_login(request: Request, user_id: int = Form(...), first_name: str = Form("Test")):
    if not config.DEV_LOGIN:
        raise HTTPException(404)
    db.upsert_user(user_id, first_name, None)
    request.session["uid"] = user_id
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/dev/demo")
def dev_demo(request: Request):
    """Development only: load demo signals and add the demo tickers to the current user's watchlist."""
    user = current_user(request)
    if not config.DEV_LOGIN:
        raise HTTPException(404)
    if not user:
        return _home()
    from app import demo
    demo.seed(user["id"])
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _home()


# ---------------- dashboard ----------------

@app.get("/dashboard")
def dashboard(request: Request, t: str | None = None):
    user = current_user(request)
    if not user:
        return _home()
    wl = db.watchlist(user["id"])
    active = t.upper() if t and t.upper() in wl else None
    c = db.get()
    if active:
        # a single ticker tab also shows low-score (archive) signals
        signals = c.execute(
            """SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
               WHERE s.ticker=? AND s.status='approved' ORDER BY s.created_at DESC LIMIT 100""", (active,)).fetchall()
    elif wl:
        q = ",".join("?" * len(wl))
        signals = c.execute(
            f"""SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
                WHERE s.ticker IN ({q}) AND s.status='approved' AND s.tier!='archive'
                ORDER BY s.created_at DESC LIMIT 100""", wl).fetchall()
    else:
        signals = []
    feedback = {r["signal_id"]: r["useful"] for r in
                c.execute("SELECT signal_id, useful FROM feedback WHERE user_id=?", (user["id"],))}
    universe = c.execute("SELECT symbol, name, exchange FROM tickers ORDER BY symbol").fetchall()
    names = {u["symbol"]: u["name"] for u in universe}
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "watchlist": wl, "active": active, "signals": signals, "feedback": feedback,
        "universe": [u for u in universe if u["symbol"] not in wl], "names": names,
        "bot_link": f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start=connect" if config.TELEGRAM_BOT_USERNAME else None,
        "admin": is_admin(user["id"]), "dev_login": config.DEV_LOGIN,
    })


@app.post("/watchlist/add")
def watchlist_add(request: Request, symbol: str = Form(...)):
    user = current_user(request)
    if not user:
        return _home()
    db.add_to_watchlist(user["id"], symbol)
    return RedirectResponse(f"/dashboard?t={symbol.upper()}", status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(request: Request, symbol: str = Form(...)):
    user = current_user(request)
    if not user:
        return _home()
    db.remove_from_watchlist(user["id"], symbol)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings")
def settings(request: Request, alert_threshold: int = Form(...), paused: str | None = Form(None)):
    user = current_user(request)
    if not user:
        return _home()
    db.set_user(user["id"], alert_threshold=max(1, min(alert_threshold, 20)), paused=1 if paused else 0)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/feedback/{signal_id}/{useful}")
def web_feedback(request: Request, signal_id: int, useful: int):
    user = current_user(request)
    if not user:
        return _home()
    if db.get().execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone() is None:
        raise HTTPException(404)
    with db.tx() as c:
        c.execute("""INSERT INTO feedback VALUES (?,?,?,?) ON CONFLICT(signal_id,user_id)
                     DO UPDATE SET useful=excluded.useful""", (signal_id, user["id"], 1 if useful else 0, db.now()))
    return RedirectResponse(request.headers.get("referer", "/dashboard"), status_code=303)


# ---------------- admin review ----------------

REVIEWER_LABEL = "Reviewer adjustment"


def _admin_or_403(request: Request):
    user = current_user(request)
    if not user or not is_admin(user["id"]):
        raise HTTPException(403)
    return user


def _back(msg: str = ""):
    from urllib.parse import quote
    return RedirectResponse("/admin" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@app.get("/admin")
def admin(request: Request):
    user = current_user(request)
    if not user or not is_admin(user["id"]):
        return _home()
    c = db.get()
    pending = c.execute(
        """SELECT s.*, d.url, d.title AS doc_title FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status='pending_review' ORDER BY s.score DESC, s.created_at""").fetchall()
    reach = {s["id"]: {m: len(publish.recipients(s["ticker"], s["score"], m)) for m in ("watchers", "broadcast")}
             for s in pending}
    recent = c.execute(
        """SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status!='pending_review' ORDER BY s.created_at DESC LIMIT 30""").fetchall()
    stats = {
        "docs": c.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall(),
        "feedback": c.execute("SELECT SUM(useful) up, COUNT(*) n FROM feedback").fetchone(),
        "usage": c.execute("""SELECT stage, model, SUM(input_tokens) i, SUM(output_tokens) o, COUNT(*) calls
                              FROM ai_usage GROUP BY stage, model""").fetchall(),
        "requests": c.execute("""SELECT symbol, COUNT(*) n FROM ticker_requests GROUP BY symbol
                                 ORDER BY n DESC LIMIT 10""").fetchall(),
        "users": c.execute("SELECT COUNT(*) n, SUM(can_message) reachable FROM users").fetchone(),
    }
    announcements = c.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 5").fetchall()
    # Documents the classifier called noise. Checking a sample catches missed events, which no other check does.
    noise = c.execute("""SELECT * FROM documents WHERE status='noise' ORDER BY id DESC LIMIT 15""").fetchall()
    return templates.TemplateResponse(request, "admin.html", {
        "user": user, "admin": True, "pending": pending, "reach": reach, "recent": recent, "stats": stats,
        "announcements": announcements, "noise": noise, "msg": request.query_params.get("msg"),
        "universe": c.execute("SELECT symbol, name FROM tickers ORDER BY symbol").fetchall(),
        "event_types": scoring.EVENT_TYPES,
    })


@app.post("/admin/signal/{signal_id}/edit")
def admin_edit(request: Request, signal_id: int, headline: str = Form(...),
               adjustment: int = Form(0), reason: str = Form("")):
    _admin_or_403(request)
    s = db.get().execute("SELECT * FROM signals WHERE id=? AND status='pending_review'", (signal_id,)).fetchone()
    if s is None:
        return _back("That signal is no longer in the queue.")
    headline = headline.strip()
    if not headline:
        return _back("The headline can't be empty.")
    if adjustment and not reason.strip():
        return _back("Add a reason for the score adjustment. Users see it in the breakdown.")
    parts = [p for p in json.loads(s["breakdown"]) if not p[0].startswith(REVIEWER_LABEL)]
    if adjustment:
        parts.append([f"{REVIEWER_LABEL}: {reason.strip()}", int(adjustment)])
    score = sum(p for _, p in parts)
    with db.tx() as c:
        c.execute("UPDATE signals SET headline=?, breakdown=?, score=?, tier=? WHERE id=?",
                  (headline, json.dumps(parts), score, scoring.tier_for(score), signal_id))
    return _back(f"Saved. Score is now {score}.")


@app.post("/admin/signal/{signal_id}/{action}")
def admin_action(request: Request, signal_id: int, action: str):
    user = _admin_or_403(request)
    delivery = {"send": "watchers", "feed": "feed_only", "broadcast": "broadcast"}.get(action)
    if action not in {"send", "feed", "broadcast", "reject"}:
        raise HTTPException(404)
    with db.tx() as c:
        cur = c.execute(
            "UPDATE signals SET status=?, reviewed_by=?, delivery=COALESCE(?, delivery) "
            "WHERE id=? AND status='pending_review'",
            ("rejected" if action == "reject" else "approved", user["id"], delivery, signal_id))
    if not cur.rowcount:
        return _back("That signal is no longer in the queue.")
    if action == "reject":
        return _back("Rejected. Nothing was sent.")
    sent = publish.publish_signal(signal_id)
    if action == "feed":
        return _back("Added to the dashboard feed. Nothing was sent to Telegram.")
    return _back(f"Sent to {sent} user{'s' if sent != 1 else ''}.")


@app.post("/admin/new")
def admin_new(request: Request, ticker: str = Form(...), url: str = Form(...), text: str = Form(...),
              title: str = Form(""), event_type: str = Form("auto")):
    _admin_or_403(request)
    from app import collectors, pipeline
    url, text = url.strip(), text.strip()
    if not db.ticker_exists(ticker):
        return _back(f"{ticker} is not in universe.yaml.")
    if not url.startswith(("http://", "https://")):
        return _back("Add the link to the original source. Every signal needs one.")
    if len(text) < 80:
        return _back("Paste the full text of the document, not just a summary.")
    if not collectors.save_document(ticker.upper(), "manual", url, title.strip(), None, text):
        old = db.get().execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
        if old["status"] not in ("noise", "failed"):
            return _back("A signal from that link already exists.")
        with db.tx() as c:  # a reviewer is retrying a document the AI skipped or failed on
            c.execute("UPDATE documents SET ticker=?, title=?, text=?, status='new' WHERE id=?",
                      (ticker.upper(), title.strip(), text, old["id"]))
    doc = db.get().execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    try:
        signal_id = pipeline.process_document(doc, event_type=event_type, force_review=True, skip_dedupe=True)
    except Exception as exc:
        with db.tx() as c:
            c.execute("UPDATE documents SET status='failed' WHERE id=?", (doc["id"],))
        return _back(f"Processing failed: {exc}")
    if signal_id is None:
        return _back("The AI classified this as noise, so no signal was created. "
                     "If you disagree, submit it again and choose the event type yourself.")
    return _back("Signal created. Review it below before sending.")


@app.post("/admin/noise/{doc_id}/promote")
def admin_promote_noise(request: Request, doc_id: int, event_type: str = Form(...)):
    """A reviewer disagrees with the classifier: process the document as a real event."""
    _admin_or_403(request)
    from app import pipeline
    doc = db.get().execute("SELECT * FROM documents WHERE id=? AND status='noise'", (doc_id,)).fetchone()
    if doc is None:
        return _back("That document is not in the noise list.")
    if event_type not in scoring.EVENT_TYPES:
        return _back("Choose an event type.")
    try:
        signal_id = pipeline.process_document(doc, event_type=event_type, force_review=True, skip_dedupe=True)
    except Exception as exc:
        return _back(f"Processing failed: {exc}")
    return _back("Signal created from a document the AI had skipped. Review it above."
                 if signal_id else "Nothing could be extracted from that document.")


@app.get("/admin/tuning")
def admin_tuning(request: Request):
    """How often users call signals useful, by event type and by scoring rule. Evidence for changing weights."""
    user = current_user(request)
    if not user or not is_admin(user["id"]):
        return _home()
    c = db.get()
    by_event = c.execute(
        """SELECT s.event_type, COUNT(*) n, SUM(f.useful) up FROM feedback f JOIN signals s ON s.id=f.signal_id
           GROUP BY s.event_type ORDER BY n DESC""").fetchall()
    rules: dict[str, list[int]] = {}
    rows = c.execute("""SELECT s.breakdown, f.useful FROM feedback f JOIN signals s ON s.id=f.signal_id""").fetchall()
    for r in rows:
        for label, _pts in json.loads(r["breakdown"]):
            key = label.split(":")[0].split("(")[0].strip()
            stat = rules.setdefault(key, [0, 0])
            stat[0] += 1
            stat[1] += r["useful"]
    by_rule = sorted(([k] + v for k, v in rules.items()), key=lambda x: -x[1])
    unrated = c.execute("""SELECT COUNT(*) n FROM signals s WHERE s.published_at IS NOT NULL
                           AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.signal_id=s.id)""").fetchone()["n"]
    return templates.TemplateResponse(request, "tuning.html", {
        "user": user, "admin": True, "by_event": by_event, "by_rule": by_rule, "unrated": unrated,
        "event_types": scoring.EVENT_TYPES})


@app.post("/admin/announce")
def admin_announce(request: Request, text: str = Form(...)):
    user = _admin_or_403(request)
    text = text.strip()
    if not text:
        return _back("Write the announcement first.")
    sent = publish.send_announcement(text, user["id"])
    return _back(f"Announcement sent to {sent} user{'s' if sent != 1 else ''}.")


# ---------------- Telegram webhook ----------------

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not config.TELEGRAM_WEBHOOK_SECRET or x_telegram_bot_api_secret_token != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403)
    bot.handle_update(await request.json())
    return {"ok": True}
