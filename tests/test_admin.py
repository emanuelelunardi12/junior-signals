import json

from fastapi.testclient import TestClient

from app import bot, collectors, config, db, pipeline
from tests.test_pipeline import DOC, fake_calls


def admin_client(monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", {1})
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": 1, "first_name": "Admin"})
    return c


def make_pending(monkeypatch):
    fake_calls(monkeypatch)
    collectors.save_document("EXMP.V", "manual", "https://example.com/p", "t", None, DOC)
    pipeline.run(collect=False)
    return db.get().execute("SELECT * FROM signals").fetchone()


def add_users():
    for uid, ticker in [(10, "EXMP.V"), (11, "DEMO"), (12, None)]:
        bot.handle_command({"id": uid, "first_name": "U"}, uid, "/start")
        if ticker:
            db.add_to_watchlist(uid, ticker)


def test_non_admin_blocked(monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", {1})
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": 99})
    assert c.post("/admin/announce", data={"text": "hi"}).status_code == 403


def test_edit_adds_visible_adjustment(monkeypatch):
    s = make_pending(monkeypatch)
    c = admin_client(monkeypatch)
    r = c.post(f"/admin/signal/{s['id']}/edit", data={"headline": "New headline", "adjustment": 2, "reason": "second strategic stake"})
    assert "Score is now" in r.text
    s2 = db.get().execute("SELECT * FROM signals WHERE id=?", (s["id"],)).fetchone()
    assert s2["score"] == s["score"] + 2 and s2["headline"] == "New headline"
    assert json.loads(s2["breakdown"])[-1] == ["Reviewer adjustment: second strategic stake", 2]
    # editing again replaces the adjustment instead of stacking it
    c.post(f"/admin/signal/{s['id']}/edit", data={"headline": "New headline", "adjustment": -1, "reason": "x"})
    assert db.get().execute("SELECT score FROM signals WHERE id=?", (s["id"],)).fetchone()["score"] == s["score"] - 1


def test_adjustment_needs_reason(monkeypatch):
    s = make_pending(monkeypatch)
    r = admin_client(monkeypatch).post(f"/admin/signal/{s['id']}/edit", data={"headline": "h", "adjustment": 3})
    assert "Add a reason" in r.text


def test_delivery_modes(monkeypatch, fresh_db):
    add_users()
    c = admin_client(monkeypatch)
    s = make_pending(monkeypatch)
    fresh_db.clear()
    assert "Sent to 1 user" in c.post(f"/admin/signal/{s['id']}/send").text
    assert [uid for uid, _ in fresh_db] == [10]


def test_feed_only_sends_nothing(monkeypatch, fresh_db):
    add_users()
    c = admin_client(monkeypatch)
    s = make_pending(monkeypatch)
    fresh_db.clear()
    c.post(f"/admin/signal/{s['id']}/feed")
    assert fresh_db == []
    assert db.get().execute("SELECT status, delivery FROM signals").fetchone()[:] == ("approved", "feed_only")


def test_broadcast_reaches_everyone(monkeypatch, fresh_db):
    add_users()
    c = admin_client(monkeypatch)
    s = make_pending(monkeypatch)
    fresh_db.clear()
    c.post(f"/admin/signal/{s['id']}/broadcast")
    assert sorted(uid for uid, _ in fresh_db) == [10, 11, 12]


def test_create_signal_from_admin(monkeypatch):
    fake_calls(monkeypatch)
    c = admin_client(monkeypatch)
    r = c.post("/admin/new", data={"ticker": "EXMP.V", "url": "https://sedarplus.ca/x", "title": "Stake",
                                   "text": DOC, "event_type": "strategic_investment"})
    assert "Signal created" in r.text
    s = db.get().execute("SELECT * FROM signals").fetchone()
    assert s["status"] == "pending_review"
    assert c.post("/admin/new", data={"ticker": "EXMP.V", "url": "https://sedarplus.ca/x", "text": DOC}).text.count("already exists") == 1


def test_create_signal_requires_source_link(monkeypatch):
    c = admin_client(monkeypatch)
    assert "link to the original source" in c.post("/admin/new", data={"ticker": "EXMP.V", "url": "none", "text": DOC}).text


def test_announcement(monkeypatch, fresh_db):
    add_users()
    c = admin_client(monkeypatch)
    fresh_db.clear()
    assert "sent to 3 users" in c.post("/admin/announce", data={"text": "We now cover DEMO."}).text
    assert db.get().execute("SELECT recipients FROM announcements").fetchone()["recipients"] == 3


def test_old_database_gets_new_column():
    conn = db.connect(":memory:")
    conn.executescript(db.SCHEMA.replace(",\n    delivery     TEXT NOT NULL DEFAULT 'watchers'   -- watchers | feed_only | broadcast", ""))
    db.init(conn)
    assert "delivery" in {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
