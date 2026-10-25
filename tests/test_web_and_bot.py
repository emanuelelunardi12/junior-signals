from fastapi.testclient import TestClient

from app import bot, config, db


def client(monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    from app.web import app
    return TestClient(app)


def test_dashboard_flow(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/").status_code == 200
    r = c.post("/dev-login", data={"user_id": 55, "first_name": "Test"})
    assert r.status_code == 200 and "Add your first ticker" in r.text
    r = c.post("/watchlist/add", data={"symbol": "EXMP.V"})
    assert "EXMP.V" in r.text
    assert c.get("/admin").status_code == 200


def test_webhook_requires_secret(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    c = client(monkeypatch)
    assert c.post("/telegram/webhook", json={}).status_code == 403
    upd = {"message": {"from": {"id": 9, "first_name": "Bo"}, "chat": {"id": 9, "type": "private"}, "text": "/start"}}
    r = c.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert r.status_code == 200 and db.get_user(9)["can_message"] == 1


def test_bot_commands(fresh_db):
    bot.handle_command({"id": 3, "first_name": "Cy"}, 3, "/start")
    bot.handle_command({"id": 3}, 3, "/add NOPE")
    assert db.get().execute("SELECT symbol FROM ticker_requests").fetchone()["symbol"] == "NOPE"
    bot.handle_command({"id": 3}, 3, "/threshold 6")
    assert db.get_user(3)["alert_threshold"] == 6
    bot.handle_callback({"id": "x", "data": "fb:999:1", "from": {"id": 3}})  # unknown signal: ignored, no crash
    bot.handle_command({"id": 3}, 3, "/stop")
    assert db.get_user(3) is None


def test_dev_login_is_on_when_nothing_is_configured(monkeypatch):
    """A fresh checkout with no .env must still be usable locally (regression)."""
    import importlib

    for var in ("DEV_LOGIN", "TELEGRAM_BOT_TOKEN", "BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("app.config.ENV_FILE", __import__("pathlib").Path("/nonexistent/.env"))
    fresh = importlib.reload(importlib.import_module("app.config"))
    try:
        assert fresh.DEV_LOGIN is True and fresh.BASE_URL.startswith("http://localhost")
        # but an explicit setting always wins
        monkeypatch.setenv("DEV_LOGIN", "0")
        assert importlib.reload(fresh).DEV_LOGIN is False
    finally:
        monkeypatch.setenv("DEV_LOGIN", "1")
        monkeypatch.setenv("BASE_URL", "http://testserver")
        importlib.reload(fresh)


def test_load_demo_button(monkeypatch):
    c = client(monkeypatch)
    r = c.post("/dev-login", data={"user_id": 77, "first_name": "Demo"})
    assert "Load demo data" in r.text
    r = c.post("/dev/demo")
    assert "Big Copper Mining buys a 9.9% stake" in r.text and "approximately 9.9%" in r.text
    assert set(db.watchlist(77)) == {"EXMP.V", "DEMO"}
    c.post("/dev/demo")  # running it twice adds nothing new
    assert db.get().execute("SELECT COUNT(*) n FROM signals").fetchone()["n"] == 4


def test_demo_button_hidden_in_production(monkeypatch):
    c = client(monkeypatch)
    c.post("/dev-login", data={"user_id": 78})
    monkeypatch.setattr(config, "DEV_LOGIN", False)
    assert c.post("/dev/demo").status_code == 404
