# Fixed settings for the test run, set before app.config loads. load_dotenv does not override these,
# so the suite behaves the same with or without a local .env file.
import os

os.environ["BASE_URL"] = "http://testserver"     # https_only cookies would break the test client
os.environ["DEV_LOGIN"] = "1"
os.environ["RUN_SCHEDULER"] = "0"
os.environ["PRICE_SOURCE"] = "none"
os.environ["DATABASE_PATH"] = "data/test.db"     # never touched: tests use an in-memory database

import pytest

from app import db


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    conn = db.connect(":memory:")
    db.init(conn)
    db.use(conn)
    db.sync_universe()
    sent = []
    # never call the real Telegram API in tests
    monkeypatch.setattr("app.telegram.send_message", lambda chat_id, text, buttons=None: sent.append((chat_id, text)))
    monkeypatch.setattr("app.telegram.answer_callback", lambda *a, **k: None)
    yield sent
    db.use(None)
