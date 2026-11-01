"""Security pass: headers, cross-site protection, production guards, error handling, admin protection."""
import importlib

import pytest
from fastapi.testclient import TestClient

from app import bot, config, db, security


def client(monkeypatch, uid=None, admins=None):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", admins if admins is not None else {1})
    from app.web import app
    c = TestClient(app)
    if uid is not None:
        c.post("/dev-login", data={"user_id": uid, "first_name": "T"})
    return c


def test_security_headers_on_every_page(monkeypatch):
    r = client(monkeypatch).get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' https://telegram.org" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]      # no inline script allowed
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "onsubmit=" not in client(monkeypatch, uid=1).get("/admin").text    # confirms moved out of inline handlers


def test_cross_site_posts_are_refused(monkeypatch):
    c = client(monkeypatch, uid=5)
    evil = c.post("/settings", data={"alert_threshold": 3}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and db.get_user(5)["alert_threshold"] == 8
    assert c.post("/settings", data={"alert_threshold": 3}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/settings", data={"alert_threshold": 3}, headers={"Origin": "null"}).status_code == 403
    ok = c.post("/settings", data={"alert_threshold": 3}, headers={"Origin": "http://testserver",
                                                                   "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200 and db.get_user(5)["alert_threshold"] == 3


def test_oversized_body_is_refused(monkeypatch):
    c = client(monkeypatch, uid=5)
    r = c.post("/settings", content=b"x", headers={"Content-Length": str(security.MAX_BODY + 1)})
    assert r.status_code == 413


@pytest.fixture
def reload_config(monkeypatch):
    def load(**env):
        for k in ("DEV_LOGIN", "BASE_URL", "RENDER_EXTERNAL_URL", "SESSION_SECRET", "TELEGRAM_BOT_TOKEN",
                  "TELEGRAM_WEBHOOK_SECRET"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(config)
    yield load
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    importlib.reload(config)


def test_test_login_can_never_be_on_for_a_public_deployment(reload_config):
    cfg = reload_config(DEV_LOGIN="1", BASE_URL="https://junior-signals.onrender.com")
    assert cfg.PUBLIC and cfg.DEV_LOGIN is False and cfg.DEV_LOGIN_BLOCKED
    assert any("SESSION_SECRET" in p for p in cfg.deployment_problems())
    cfg = reload_config(RENDER_EXTERNAL_URL="https://js.onrender.com", SESSION_SECRET="x" * 40,
                        TELEGRAM_BOT_TOKEN="1:abc", TELEGRAM_WEBHOOK_SECRET="s" * 32)
    assert cfg.BASE_URL == "https://js.onrender.com" and cfg.deployment_problems() == []
    cfg = reload_config(BASE_URL="https://x.example", SESSION_SECRET="y" * 40, TELEGRAM_BOT_TOKEN="1:abc")
    assert any("WEBHOOK_SECRET" in p for p in cfg.deployment_problems())


def test_everyone_is_not_admin_on_a_public_url(monkeypatch):
    from app import web
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", set())
    assert web.is_admin(42) is True                   # local convenience only
    monkeypatch.setattr(config, "PUBLIC", True)
    assert web.is_admin(42) is False


ADMIN_POSTS = ["/admin/signal/1/edit", "/admin/signal/1/retract", "/admin/signal/1/send", "/admin/new",
               "/admin/noise/1/promote", "/admin/announce", "/admin/tuning/run", "/admin/tuning/1/approve",
               "/admin/insights/run", "/admin/insights/1/propose/0", "/admin/listings", "/admin/webhook"]


def test_every_admin_action_is_protected(monkeypatch):
    c = client(monkeypatch, uid=99)                  # logged in, not an admin
    for path in ADMIN_POSTS:
        assert c.post(path, data={"headline": "h", "ticker": "EXMP.V", "url": "https://x.y", "text": "t" * 90,
                                  "event_type": "private_placement", "reason": "r"}).status_code == 403, path
    for page in ("/admin", "/admin/tuning", "/admin/insights"):
        r = c.get(page)
        assert "Live now" not in r.text and "Proposed changes" not in r.text and "AI insights</h1>" not in r.text
    anon = client(monkeypatch)
    assert anon.post("/admin/tuning/run").status_code == 403


def test_friendly_errors_without_internals(monkeypatch):
    c = client(monkeypatch)
    r = c.get("/no-such-page")
    assert r.status_code == 404 and "doesn" in r.text and "Traceback" not in r.text
    assert c.get("/api/nothing").json() == {"error": "That page doesn't exist."}
    from app.web import app
    quiet = TestClient(app, raise_server_exceptions=False)

    def boom():
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(db, "get", boom)
    r = quiet.get("/healthz")
    assert r.status_code == 500 and "secret internal detail" not in r.text


def test_health_check(monkeypatch):
    assert client(monkeypatch).get("/healthz").json() == {"ok": True}


def test_webhook_tolerates_bad_input_but_not_bad_secrets(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    c = client(monkeypatch)
    h = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    assert c.post("/telegram/webhook", content=b"{not json", headers=h).status_code == 200
    assert c.post("/telegram/webhook", json=[1, 2], headers=h).status_code == 200
    assert c.post("/telegram/webhook", json={"callback_query": {"data": "fb:x"}}, headers=h).status_code == 200
    assert c.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cre"}).status_code == 403
    assert c.post("/telegram/webhook", json={}, headers={**h, "Origin": "https://evil.example"}).status_code == 200


def test_login_is_rate_limited(monkeypatch):
    monkeypatch.setitem(security.limiter.limits, "login", (2, 60))
    c = client(monkeypatch)
    for _ in range(3):
        r = c.get("/auth/telegram?id=1&auth_date=1&hash=x", follow_redirects=False)
    assert r.status_code == 303 and "error=login" in r.headers["location"]


def test_bot_input_is_bounded(fresh_db):
    bot.handle_command({"id": 3, "first_name": "Cy"}, 3, "/start")
    bot.handle_command({"id": 3}, 3, "/threshold 999")
    assert db.get_user(3)["alert_threshold"] == 20
    fresh_db.clear()
    bot.handle_command({"id": 3}, 3, "/add <b>x</b>")
    assert "doesn't look like a ticker" in fresh_db[-1][1]
    bot.handle_command({"id": 3}, 3, "/tracking off")
    assert db.get_user(3)["tracking_opt_out"] == 1
