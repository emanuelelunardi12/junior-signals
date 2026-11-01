"""Web dashboard + Telegram webhook. Run with: uvicorn app.web:app --reload"""
import hmac
import json
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import behaviour, bot, config, db, listings, publish, scoring, security, telegram

HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    print(config.describe())
    problems = config.deployment_problems()
    if problems:
        raise RuntimeError("Unsafe settings for a public deployment:\n- " + "\n- ".join(problems))
    db.get()
    db.sync_universe()
    if config.LISTINGS_AUTOLOAD and not db.get().execute("SELECT 1 FROM listings WHERE source='sec' LIMIT 1").fetchone():
        import threading
        from app import listings
        threading.Thread(target=listings.fetch_sec, name="sec-listings", daemon=True).start()
    sched = None
    if config.RUN_SCHEDULER:
        from app import scheduler
        sched = scheduler.start()
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="Junior Signals", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def protect(request: Request, call_next):
    """Body size cap, same-origin check for state-changing requests, security headers on every response."""
    if int(request.headers.get("content-length") or 0) > security.MAX_BODY:
        return PlainTextResponse("Request too large", status_code=413)
    if not security.same_origin(request, config.BASE_URL):
        return PlainTextResponse("Cross-site request refused", status_code=403)
    response = await call_next(request)
    security.add_headers(response, https=config.PUBLIC)
    return response


app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 24 * 30,
                   https_only=config.PUBLIC, same_site="lax", session_cookie="js_session")
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
templates.env.globals["lead_quote"] = telegram.lead_quote
templates.env.globals["source_domain"] = telegram.source_domain
templates.env.globals["event_types"] = scoring.EVENT_TYPES


def pct(x) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


templates.env.filters["pct"] = pct


def current_user(request: Request):
    uid = request.session.get("uid")
    return db.get_user(uid) if uid else None


def is_admin(uid: int) -> bool:
    """Founders listed in ADMIN_IDS. Locally, with the test login on and no ADMIN_IDS, everyone is admin
    (DEV_LOGIN can never be on for a public https deployment, see config.py)."""
    return uid in config.ADMIN_IDS or (config.DEV_LOGIN and not config.ADMIN_IDS and not config.PUBLIC)


def _home():
    return RedirectResponse("/", status_code=303)


# ---------------- errors and health ----------------

ERROR_TEXT = {403: "You don't have access to that page.", 404: "That page doesn't exist.",
              405: "That action isn't available here.", 413: "That was too large.", 429: "Too many requests. Slow down a little."}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    msg = ERROR_TEXT.get(exc.status_code, "Something went wrong.")
    if request.url.path.startswith("/api/") or _wants_json(request):
        return JSONResponse({"error": msg}, status_code=exc.status_code)
    user = current_user(request) if "session" in request.scope else None
    return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "message": msg, "user": user},
                                      status_code=exc.status_code)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    traceback.print_exception(exc)   # details go to the server log, never to the browser
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Server error"}, status_code=500)
    return templates.TemplateResponse(request, "error.html", {"status": 500, "user": None,
                                      "message": "Something went wrong on our side. Try again in a minute."},
                                      status_code=500)


@app.get("/healthz")
def healthz():
    """For the host's health check: the process is up and the database answers."""
    db.get().execute("SELECT 1").fetchone()
    return {"ok": True}


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
    request.session.clear()
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
    if _limited(request, "login"):
        return RedirectResponse("/?error=login", status_code=303)
    data = telegram.verify_login(dict(request.query_params))
    if data is None:
        return RedirectResponse("/?error=login", status_code=303)
    _start_session(request, int(data["id"]), data.get("first_name"), data.get("username"))
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/dev-login")
def dev_login(request: Request, user_id: int = Form(...), first_name: str = Form("Test")):
    if not config.DEV_LOGIN:
        raise HTTPException(404)
    db.upsert_user(user_id, first_name[:64], None)
    request.session.clear()
    request.session["uid"] = user_id
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/dev/demo")
def dev_demo(request: Request):
    """Development only: load demo signals and add the demo tickers to the current user's watchlist."""
    user = current_user(request)
    if not config.DEV_LOGIN or config.PUBLIC:
        raise HTTPException(404)
    if not user:
        return _home()
    from app import demo
    demo.seed(user["id"])
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/privacy")
def privacy(request: Request):
    """Plain-language note on what behaviour we record, why, and how to opt out. Public."""
    user = current_user(request)
    return templates.TemplateResponse(request, "privacy.html", {
        "user": user, "admin": bool(user and is_admin(user["id"])), "retention_days": behaviour_retention_days(),
        "gpc": request.headers.get("sec-gpc", "").strip() == "1"})


def behaviour_retention_days() -> int:
    return config.BEHAVIOUR_RETENTION_DAYS


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _home()


# ---------------- dashboard ----------------

TICKER_RE = __import__("re").compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def clean_symbol(raw: str | None) -> str | None:
    """Tickers are short uppercase symbols (EXMP.V, ABX.TO, BHP). Anything else is rejected."""
    sym = (raw or "").strip().upper()
    return sym if TICKER_RE.match(sym) else None


def _limited(request: Request, bucket: str) -> bool:
    return not security.limiter.allow(bucket, security.client_key(request))


def _too_many():
    return JSONResponse({"error": "Too many requests. Slow down a little."}, status_code=429)


@app.get("/dashboard")
def dashboard(request: Request, t: str | None = None, type: str | None = None, q: str | None = None):
    user = current_user(request)
    if not user:
        return _home()
    sid = behaviour.web_session_id(user["id"])
    track = behaviour.tracking_allowed(user, request.headers)
    wl = db.watchlist(user["id"])
    sym = clean_symbol(t)
    active = sym if sym in wl else None
    etype = type if type in scoring.EVENT_TYPES else None
    if etype and track and request.session.get("last_filter") != etype:
        behaviour.record_watchlist(sid, "web", "filter", event_type=etype)
    request.session["last_filter"] = etype
    c = db.get()
    type_sql, type_args = (" AND s.event_type=?", [etype]) if etype else ("", [])
    if active:
        # a single ticker tab also shows low-score (archive) signals
        signals = c.execute(
            f"""SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
               WHERE s.ticker=? AND s.status='published' {type_sql} ORDER BY s.created_at DESC LIMIT 100""",
            [active] + type_args).fetchall()
    elif wl:
        marks = ",".join("?" * len(wl))
        signals = c.execute(
            f"""SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
                WHERE s.ticker IN ({marks}) AND s.status='published' AND s.tier!='archive' {type_sql}
                ORDER BY s.created_at DESC LIMIT 100""", wl + type_args).fetchall()
    else:
        signals = []
    shown = [s["id"] for s in signals] + [i for i in request.session.get("shown", []) if isinstance(i, int)]
    request.session["shown"] = list(dict.fromkeys(shown))[:200]   # events are accepted only for these
    feedback = {r["signal_id"]: r["useful"] for r in
                c.execute("SELECT signal_id, useful FROM feedback WHERE user_id=?", (user["id"],))}
    names = {r["symbol"]: r["name"] for r in c.execute("SELECT symbol, name FROM tickers")}
    query = (q or "").strip()[:40]
    results = [] if not query or _limited(request, "search") else \
        [r for r in listings.search(query) if r["symbol"] not in wl]
    curated = [dict(r, coverage=listings.coverage(r["symbol"])) for r in c.execute(
        "SELECT symbol, name, exchange FROM tickers WHERE configured=1 AND rss!='[]' ORDER BY symbol LIMIT 12")
        if r["symbol"] not in wl]
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "watchlist": wl, "active": active, "signals": signals, "feedback": feedback,
        "results": results, "q": query, "curated": curated, "names": names, "etype": etype,
        "coverage": listings.coverage(active) if active else None,
        "bot_link": f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start=connect" if config.TELEGRAM_BOT_USERNAME else None,
        "admin": is_admin(user["id"]), "dev_login": config.DEV_LOGIN, "track": track,
        "msg": request.query_params.get("msg"),
    })


def _dash(msg: str = "", t: str | None = None):
    from urllib.parse import urlencode
    q = {k: v for k, v in (("t", t), ("msg", msg)) if v}
    return RedirectResponse("/dashboard" + (f"?{urlencode(q)}" if q else ""), status_code=303)


@app.post("/watchlist/add")
def watchlist_add(request: Request, symbol: str = Form(...)):
    user = current_user(request)
    if not user:
        return _home()
    if _limited(request, "watchlist"):
        return _dash("Too many changes in a minute. Try again shortly.")
    sym = clean_symbol(symbol)
    if not sym:
        return _dash("That doesn't look like a ticker symbol.")
    added = db.add_to_watchlist(user["id"], sym)
    if not added:
        return _dash(f"{sym} is not on TSX, TSX Venture, NASDAQ or NYSE in our list. Request noted.")
    if behaviour.tracking_allowed(user, request.headers):
        behaviour.record_watchlist(behaviour.web_session_id(user["id"]), "web", "add", ticker=sym)
    cov = listings.coverage(sym)
    if cov == "not covered yet":
        return _dash(f"Added {sym}. We don't collect its news automatically yet, so only signals we add by hand "
                     "will appear. Request noted.", t=sym)
    return _dash(t=sym)


@app.post("/watchlist/remove")
def watchlist_remove(request: Request, symbol: str = Form(...)):
    user = current_user(request)
    if not user:
        return _home()
    if _limited(request, "watchlist"):
        return _dash("Too many changes in a minute. Try again shortly.")
    sym = clean_symbol(symbol)
    if sym and sym in db.watchlist(user["id"]):
        db.remove_from_watchlist(user["id"], sym)
        if behaviour.tracking_allowed(user, request.headers):
            behaviour.record_watchlist(behaviour.web_session_id(user["id"]), "web", "remove", ticker=sym)
    return _dash()


@app.post("/settings")
def settings(request: Request, alert_threshold: int = Form(...), paused: str | None = Form(None),
             no_tracking: str | None = Form(None)):
    user = current_user(request)
    if not user:
        return _home()
    if _limited(request, "settings"):
        return _dash("Too many changes in a minute. Try again shortly.")
    db.set_user(user["id"], alert_threshold=max(1, min(alert_threshold, 20)), paused=1 if paused else 0,
                tracking_opt_out=1 if no_tracking else 0)
    return _dash("Settings saved.")


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@app.post("/feedback/{signal_id}/{useful}")
def web_feedback(request: Request, signal_id: int, useful: int):
    """Useful (1) / Not useful (0). Tapping the active choice again clears it."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login"}, status_code=401) if _wants_json(request) else _home()
    if useful not in (0, 1):
        raise HTTPException(404)
    if _limited(request, "feedback"):
        return _too_many() if _wants_json(request) else _dash("Too many ratings in a minute.")
    if db.get().execute("SELECT 1 FROM signals WHERE id=? AND status='published'", (signal_id,)).fetchone() is None:
        raise HTTPException(404)
    state = behaviour.set_rating(user["id"], signal_id, useful)
    if behaviour.tracking_allowed(user, request.headers):
        action = "like_cleared" if state is None else ("like" if state == 1 else "dislike")
        behaviour.record_rating(behaviour.web_session_id(user["id"]), "web", signal_id, action)
    if _wants_json(request):
        return {"state": state}
    return RedirectResponse(_safe_back(request), status_code=303)


def _safe_back(request: Request) -> str:
    """Go back to the page the form was on, but only within this site."""
    from urllib.parse import urlparse
    ref = urlparse(request.headers.get("referer", ""))
    if ref.netloc and ref.netloc != request.url.netloc:
        return "/dashboard"
    path = ref.path or "/dashboard"
    if not path.startswith("/") or path.startswith("//"):
        return "/dashboard"
    return path + (f"?{ref.query}" if ref.query else "")


@app.post("/api/events")
async def api_events(request: Request):
    """Behaviour events from the dashboard (see app/static/track.js and app/behaviour.py)."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login"}, status_code=401)
    if int(request.headers.get("content-length") or 0) > 16_384:
        return JSONResponse({"error": "too large"}, status_code=413)
    if _limited(request, "events"):
        return _too_many()
    if not behaviour.tracking_allowed(user, request.headers):
        return {"stored": 0, "tracking": "off"}
    body = b""
    async for chunk in request.stream():   # count bytes as they arrive: a chunked body has no Content-Length
        body += chunk
        if len(body) > 16_384:
            return JSONResponse({"error": "too large"}, status_code=413)
    try:
        raw = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    batch = behaviour.parse_batch(raw)
    if batch is None:
        return JSONResponse({"error": "invalid events"}, status_code=422)
    shown = {i for i in request.session.get("shown", []) if isinstance(i, int)}
    return {"stored": behaviour.record_browser_events(behaviour.web_session_id(user["id"]), batch, shown)}


@app.get("/api/tickers")
def api_tickers(request: Request, q: str = ""):
    """Ticker search for the watchlist box: symbol prefix first, then company name."""
    if not current_user(request):
        return JSONResponse({"error": "login"}, status_code=401)
    if _limited(request, "search"):
        return _too_many()
    return {"results": [{k: r[k] for k in ("symbol", "exchange", "name", "coverage")} for r in listings.search(q)]}


@app.post("/comments/{signal_id}")
def comment(request: Request, signal_id: int, text: str = Form("")):
    """Optional free-text comment. Side input for founders only; not used by the learning loop."""
    user = current_user(request)
    if not user:
        return _home()
    if _limited(request, "comment"):
        return _dash("You've sent several comments already. Try again in a few minutes.")
    if len(text) > 4 * behaviour.MAX_COMMENT:
        return _dash("That comment is too long (500 characters at most).")
    ok = behaviour.save_comment(behaviour.web_session_id(user["id"]), signal_id, text)
    return _dash("Thanks, a founder will read it." if ok else "Write a short comment first.")


# ---------------- admin ----------------
# Publishing is automatic, so /admin is not a gate. It shows what went live, what the red lines withheld,
# the founder's own hand-made drafts, and lets a founder retract a live signal after the fact.

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
    drafts = c.execute(
        """SELECT s.*, d.url, d.title AS doc_title FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status='draft' ORDER BY s.score DESC, s.created_at""").fetchall()
    reach = {s["id"]: {m: len(publish.recipients(s["ticker"], s["score"], m)) for m in ("watchers", "broadcast")}
             for s in drafts}
    live = c.execute(
        """SELECT s.*, d.url, (SELECT COUNT(*) FROM deliveries x WHERE x.signal_id=s.id) AS sent
           FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status='published' ORDER BY s.published_at DESC, s.id DESC LIMIT 30""").fetchall()
    withheld = c.execute(
        """SELECT s.*, d.url FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status IN ('withheld','retracted','rejected') ORDER BY s.id DESC LIMIT 15""").fetchall()
    stats = {
        "docs": c.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall(),
        "signals": c.execute("SELECT status, COUNT(*) n FROM signals GROUP BY status").fetchall(),
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
        "listings": listings.summary(), "user": user, "admin": True, "drafts": drafts, "reach": reach, "live": live, "withheld": withheld,
        "stats": stats, "announcements": announcements, "noise": noise, "msg": request.query_params.get("msg"),
        "universe": c.execute("SELECT symbol, name FROM tickers ORDER BY symbol").fetchall(),
        "event_types": scoring.EVENT_TYPES,
    })


@app.post("/admin/signal/{signal_id}/edit")
def admin_edit(request: Request, signal_id: int, headline: str = Form(...),
               adjustment: int = Form(0), reason: str = Form("")):
    """Edit one of the founder's own drafts. Live pipeline signals are never edited in place."""
    _admin_or_403(request)
    s = db.get().execute("SELECT * FROM signals WHERE id=? AND status='draft'", (signal_id,)).fetchone()
    if s is None:
        return _back("That draft is no longer waiting.")
    headline = headline.strip()[:300]
    if not headline:
        return _back("The headline can't be empty.")
    if adjustment and not reason.strip():
        return _back("Add a reason for the score adjustment. Users see it in the breakdown.")
    from app import guardrails
    if guardrails.advice_phrase(reason):
        return _back("That reason reads as investment advice; users see it, so rephrase it as a fact.")
    adjustment = max(-10, min(int(adjustment), 10))
    old_parts, old_rules = json.loads(s["breakdown"]), json.loads(s["rule_keys"] or "[]")
    keep = [i for i, p in enumerate(old_parts) if not p[0].startswith(REVIEWER_LABEL)]
    parts = [old_parts[i] for i in keep]
    rules = [old_rules[i] for i in keep if i < len(old_rules)]
    if adjustment:
        parts.append([f"{REVIEWER_LABEL}: {reason.strip()[:120]}", adjustment])
        rules.append("reviewer_adjustment")
    score = sum(p for _, p in parts)
    with db.tx() as c:
        c.execute("UPDATE signals SET headline=?, breakdown=?, score=?, tier=?, rule_keys=? WHERE id=?",
                  (headline, json.dumps(parts), score, scoring.tier_for(score), json.dumps(rules), signal_id))
    return _back(f"Saved. Score is now {score}.")


@app.post("/admin/signal/{signal_id}/retract")
def admin_retract(request: Request, signal_id: int, reason: str = Form("")):
    """After-the-fact correction of a live signal. Not part of the alert path: the alert already went out."""
    _admin_or_403(request)
    from app import guardrails
    reason = reason.strip()[:300]
    if not reason:
        return _back("Give a reason. Everyone who received the signal gets it as a correction.")
    if guardrails.advice_phrase(reason):
        return _back("That reason reads as investment advice; users see it, so rephrase it as a fact.")
    if db.get().execute("SELECT 1 FROM signals WHERE id=? AND status='published'", (signal_id,)).fetchone() is None:
        return _back("That signal is not live.")
    sent = publish.retract(signal_id, reason)
    return _back(f"Retracted. Correction sent to {sent} user{'s' if sent != 1 else ''}.")


@app.post("/admin/signal/{signal_id}/{action}")
def admin_action(request: Request, signal_id: int, action: str):
    """Send or discard one of the founder's own drafts."""
    _admin_or_403(request)
    delivery = {"send": "watchers", "feed": "feed_only", "broadcast": "broadcast"}.get(action)
    if action not in {"send", "feed", "broadcast", "reject"}:
        raise HTTPException(404)
    user = current_user(request)
    with db.tx() as c:
        if action == "reject":
            cur = c.execute("UPDATE signals SET status='rejected', reviewed_by=? WHERE id=? AND status='draft'",
                            (user["id"], signal_id))
        else:
            cur = c.execute("UPDATE signals SET reviewed_by=?, delivery=? WHERE id=? AND status='draft'",
                            (user["id"], delivery, signal_id))
    if not cur.rowcount:
        return _back("That draft is no longer waiting.")
    if action == "reject":
        return _back("Discarded. Nothing was sent.")
    sent = publish.go_live(signal_id)
    status = db.get().execute("SELECT status, status_note FROM signals WHERE id=?", (signal_id,)).fetchone()
    if status["status"] == "withheld":
        return _back(f"Not published: {status['status_note']}")
    if action == "feed":
        return _back("Added to the dashboard feed. Nothing was sent to Telegram.")
    return _back(f"Sent to {sent} user{'s' if sent != 1 else ''}.")


@app.post("/admin/new")
def admin_new(request: Request, ticker: str = Form(...), url: str = Form(...), text: str = Form(...),
              title: str = Form(""), event_type: str = Form("auto")):
    _admin_or_403(request)
    from app import collectors, pipeline
    from app import guardrails
    url, text, title = url.strip()[:1000], text.strip(), title.strip()[:300]
    sym = clean_symbol(ticker)
    if not sym or not db.ticker_exists(sym):
        return _back(f"{ticker[:20]} is not a known listing. Load it or add it to universe.yaml first.")
    ticker = sym
    if not guardrails.is_primary_source(url):
        return _back("Add the link to the original source. Every signal needs one.")
    if len(text) < 80:
        return _back("Paste the full text of the document, not just a summary.")
    if len(text) > 300_000:
        return _back("That document is very long; paste the relevant release or filing only.")
    if not collectors.save_document(ticker.upper(), "manual", url, title.strip(), None, text):
        old = db.get().execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
        if old["status"] not in ("noise", "failed"):
            return _back("A signal from that link already exists.")
        with db.tx() as c:  # a reviewer is retrying a document the AI skipped or failed on
            c.execute("UPDATE documents SET ticker=?, title=?, text=?, status='new' WHERE id=?",
                      (ticker.upper(), title.strip(), text, old["id"]))
    doc = db.get().execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    try:
        signal_id = pipeline.process_document(doc, event_type=event_type, draft=True, skip_dedupe=True)
    except Exception as exc:
        with db.tx() as c:
            c.execute("UPDATE documents SET status='failed' WHERE id=?", (doc["id"],))
        return _back(f"Processing failed: {exc}")
    if signal_id is None:
        return _back("The AI classified this as noise, so no signal was created. "
                     "If you disagree, submit it again and choose the event type yourself.")
    return _back("Draft created. Check it above, then choose how to send it.")


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
        signal_id = pipeline.process_document(doc, event_type=event_type, draft=True, skip_dedupe=True)
    except Exception as exc:
        return _back(f"Processing failed: {exc}")
    return _back("Draft created from a document the AI had skipped. Check it above."
                 if signal_id else "Nothing could be extracted from that document.")


# ---------------- learning loop: the human sits HERE, not in the alert path ----------------

def _tuning_back(msg: str = "", page: str = "/admin/tuning"):
    from urllib.parse import quote
    return RedirectResponse(page + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@app.get("/admin/tuning")
def admin_tuning(request: Request):
    """Proposed self-modifications with their evidence, for a founder to approve or reject; weights in force;
    before/after results of approved changes; learning metrics per cycle."""
    from app import learning
    user = current_user(request)
    if not user or not is_admin(user["id"]):
        return _home()
    c = db.get()
    proposals = [dict(p, evidence=json.loads(p["evidence"])) for p in c.execute(
        "SELECT * FROM tuning_proposals WHERE status='proposed' ORDER BY id")]
    decided = c.execute("""SELECT * FROM tuning_proposals WHERE status IN ('approved','rejected','reverted','superseded')
                           ORDER BY decided_at DESC LIMIT 20""").fetchall()
    results = [dict(p, result=learning.before_after(p)) for p in decided if p["status"] in ("approved", "reverted")]
    cycles = [dict(r, metrics=json.loads(r["metrics"])) for r in c.execute(
        "SELECT * FROM learning_cycles ORDER BY id DESC LIMIT 12")]
    since = learning._ago(learning.WINDOW_DAYS)
    by_type = behaviour.engagement_by_event_type(since=since)
    by_rule = learning.engagement_by_rule(since=since)
    # older report: Useful rate by breakdown line, works for every rated signal including pre-learning ones
    lines: dict[str, list[int]] = {}
    for r in c.execute("SELECT s.breakdown, f.useful FROM feedback f JOIN signals s ON s.id=f.signal_id"):
        for label, _pts in json.loads(r["breakdown"]):
            stat = lines.setdefault(label.split(":")[0].split("(")[0].strip(), [0, 0])
            stat[0] += 1
            stat[1] += r["useful"]
    by_line = sorted(([k] + v for k, v in lines.items()), key=lambda x: -x[1])
    unrated = c.execute("""SELECT COUNT(*) n FROM signals s WHERE s.status='published'
                           AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.signal_id=s.id)""").fetchone()["n"]
    filters = c.execute("""SELECT event_type, COUNT(*) n FROM watchlist_events WHERE action='filter' AND created_at >= ?
                           GROUP BY event_type ORDER BY n DESC""", (since,)).fetchall()
    watch = c.execute("""SELECT ticker, SUM(action='add') adds, SUM(action='remove') removes FROM watchlist_events
                         WHERE action IN ('add','remove') AND created_at >= ? GROUP BY ticker
                         ORDER BY adds + removes DESC LIMIT 10""", (since,)).fetchall()
    return templates.TemplateResponse(request, "tuning.html", {"filters": filters, "watch": watch,
        "user": user, "admin": True, "proposals": proposals, "decided": decided, "results": results,
        "cycles": cycles, "by_type": by_type, "by_rule": by_rule, "by_line": by_line, "unrated": unrated,
        "weights": learning.weight_table(), "version": learning.weights_version(), "labels": scoring.RULE_LABELS,
        "rules": learning, "msg": request.query_params.get("msg"), "window": learning.WINDOW_DAYS})


@app.post("/admin/tuning/run")
def admin_tuning_run(request: Request):
    """Run a learning cycle now. It only creates proposals."""
    from app import learning
    _admin_or_403(request)
    out = learning.run_cycle()
    n = len(out["proposals"])
    return _tuning_back(f"Cycle {out['cycle_id']} done: {out['events']} events, {n} new proposal{'s' if n != 1 else ''}.")


@app.post("/admin/tuning/{proposal_id}/{action}")
def admin_tuning_decide(request: Request, proposal_id: int, action: str, note: str = Form("")):
    """THE HUMAN SAFETY CHECK: a founder approves, rejects or reverts a proposed change to the scoring weights."""
    from app import learning
    user = _admin_or_403(request)
    note = note.strip()[:500]
    try:
        if action == "approve":
            version = learning.approve(proposal_id, user["id"], note)
            return _tuning_back(f"Approved. Weights version {version} is in force for every signal scored from now on.")
        if action == "reject":
            learning.reject(proposal_id, user["id"], note)
            return _tuning_back("Rejected. Nothing changed.")
        if action == "revert":
            version = learning.revert(proposal_id, user["id"])
            return _tuning_back(f"Reverted. Weights version {version} restores the previous value.")
    except learning.NotAllowed as exc:
        return _tuning_back(str(exc))
    raise HTTPException(404)


@app.get("/admin/insights")
def admin_insights(request: Request):
    """AI-clustered behaviour patterns and suggested weights to revisit. Suggestion-only."""
    user = current_user(request)
    if not user or not is_admin(user["id"]):
        return _home()
    c = db.get()
    runs = [dict(r, output=json.loads(r["output"]) if r["output"] else None)
            for r in c.execute("SELECT * FROM ai_insights ORDER BY id DESC LIMIT 5")]
    comments = c.execute("""SELECT m.*, s.ticker, s.headline FROM comments m LEFT JOIN signals s ON s.id=m.signal_id
                            ORDER BY m.id DESC LIMIT 30""").fetchall()
    return templates.TemplateResponse(request, "insights.html", {
        "user": user, "admin": True, "runs": runs, "comments": comments, "model": config.MODEL_INSIGHT,
        "has_key": bool(config.ANTHROPIC_API_KEY), "msg": request.query_params.get("msg")})


@app.post("/admin/insights/run")
def admin_insights_run(request: Request):
    from app import insights
    _admin_or_403(request)
    if _limited(request, "insights"):
        return _tuning_back("The insight job ran a moment ago. Try again later.", "/admin/insights")
    row = insights.run()
    r = db.get().execute("SELECT status, note FROM ai_insights WHERE id=?", (row,)).fetchone()
    return _tuning_back({"ok": "Insight run finished.", "skipped": f"Skipped: {r['note']}",
                         "failed": f"Failed: {r['note']}"}[r["status"]], "/admin/insights")


@app.post("/admin/insights/{insight_id}/propose/{index}")
def admin_insight_propose(request: Request, insight_id: int, index: int):
    """Turn an AI suggestion into a normal proposal. It still has to be approved on /admin/tuning."""
    from app import insights
    _admin_or_403(request)
    pid = insights.suggestion_to_proposal(insight_id, index)
    if pid is None:
        return _tuning_back("Could not create a proposal (already open, out of range, or a 'review' suggestion).",
                            "/admin/insights")
    return _tuning_back("Proposal created from the AI suggestion. Approve or reject it here.")


@app.post("/admin/webhook")
def admin_webhook(request: Request):
    """Point Telegram at this deployment (same as `python -m app.cli set-webhook`, for hosts without a shell)."""
    _admin_or_403(request)
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_WEBHOOK_SECRET and config.PUBLIC):
        return _back("Needs TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and an https BASE_URL.")
    try:
        telegram.set_webhook(f"{config.BASE_URL}/telegram/webhook")
    except telegram.TelegramError as exc:
        return _back(f"Telegram refused: {exc}")
    return _back("Telegram webhook connected. Bot commands and button taps now reach this server.")


@app.post("/admin/listings")
async def admin_listings(request: Request):
    """Load a TSX/TSXV issuer list a founder downloaded by hand (TMX terms forbid automated download),
    or refresh the SEC list now."""
    _admin_or_403(request)
    form = await request.form()
    source = str(form.get("source", ""))
    if source == "sec":
        n = listings.fetch_sec()
        return _back(f"Loaded {n} NASDAQ/NYSE listings from the SEC." if n else "SEC download failed; see the server log.")
    exchange = str(form.get("exchange", "")).upper()
    upload = form.get("file")
    if exchange not in ("TSX", "TSXV") or upload is None or not hasattr(upload, "read"):
        return _back("Choose TSX or TSX Venture and a CSV file.")
    raw = await upload.read(5_000_001)
    if len(raw) > 5_000_000:
        return _back("That file is over 5 MB.")
    try:
        n = listings.load_tmx_csv(raw.decode("utf-8-sig", errors="replace"), exchange)
    except ValueError as exc:
        return _back(f"Could not read the file: {exc}")
    return _back(f"Loaded {n} {exchange} listings.")


@app.post("/admin/announce")
def admin_announce(request: Request, text: str = Form(...)):
    user = _admin_or_403(request)
    from app import guardrails
    text = text.strip()[:1000]
    if not text:
        return _back("Write the announcement first.")
    if guardrails.advice_phrase(text):
        return _back("That announcement reads as investment advice. Information only: rephrase it.")
    sent = publish.send_announcement(text, user["id"])
    return _back(f"Announcement sent to {sent} user{'s' if sent != 1 else ''}.")


# ---------------- Telegram webhook ----------------

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not config.TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(
            x_telegram_bot_api_secret_token.encode(), config.TELEGRAM_WEBHOOK_SECRET.encode()):
        raise HTTPException(403)
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}   # never make Telegram retry a malformed update
    if isinstance(update, dict):
        try:
            bot.handle_update(update)
        except Exception as exc:  # one bad update must not block the queue: Telegram retries non-200s
            print(f"[webhook] update failed: {exc}")
    return {"ok": True}
