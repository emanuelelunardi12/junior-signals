"""Behaviour instrumentation: what is recorded, how it is validated, and how opt-out works."""
from fastapi.testclient import TestClient

from app import behaviour, bot, collectors, config, db, pipeline, security
from tests.test_pipeline import DOC, fake_calls


def setup(monkeypatch, uid=55):
    fake_calls(monkeypatch)
    collectors.save_document("EXMP.V", "rss", "https://example.com/b1", "t", None, DOC)
    pipeline.run(collect=False)
    sig = db.get().execute("SELECT * FROM signals").fetchone()
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": uid, "first_name": "T"})
    c.post("/watchlist/add", data={"symbol": "EXMP.V"})
    return c, sig


def rows(table="behaviour_events"):
    return db.get().execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_browser_events_are_stored_with_server_side_metadata(monkeypatch):
    c, sig = setup(monkeypatch)
    r = c.post("/api/events", json={"events": [
        {"a": "impression", "s": sig["id"], "p": 0},
        {"a": "open", "s": sig["id"]},
        {"a": "dwell", "s": sig["id"], "v": 9999},          # capped
        {"a": "source_click", "s": sig["id"]},
        {"a": "scroll_past", "s": 999999},                   # unknown signal: dropped
    ]})
    assert r.json() == {"stored": 4}
    got = rows()
    assert [g["action"] for g in got] == ["impression", "open", "dwell", "source_click"]
    assert {g["event_type"] for g in got} == {"strategic_investment"} and {g["ticker"] for g in got} == {"EXMP.V"}
    assert got[2]["value"] == behaviour.MAX_DWELL and got[0]["position"] == 0
    sid = got[0]["session_id"]
    assert behaviour.valid_session_id(sid) and "55" not in sid      # anonymous: not the Telegram id
    assert {g["surface"] for g in got} == {"web"}


def test_invalid_events_are_rejected(monkeypatch):
    c, sig = setup(monkeypatch)
    assert c.post("/api/events", json={"events": [{"a": "like", "s": sig["id"]}]}).status_code == 422   # ratings can't be faked here
    assert c.post("/api/events", json={"events": [{"a": "open", "s": "1; DROP TABLE signals"}]}).status_code == 422
    assert c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}] * 51}).status_code == 422
    assert c.post("/api/events", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert c.post("/api/events", content=b"x" * 20000).status_code == 413
    assert rows() == []


def test_events_need_a_login():
    from app.web import app
    assert TestClient(app).post("/api/events", json={"events": [{"a": "open", "s": 1}]}).status_code == 401


def test_session_id_comes_from_the_server_and_rotates_at_login(monkeypatch):
    c, sig = setup(monkeypatch)
    c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}], "sid": "attacker-chosen-id-123456"})
    first = rows()[-1]["session_id"]
    assert first != "attacker-chosen-id-123456"
    c.post("/dev-login", data={"user_id": 55, "first_name": "T"})
    c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}]})
    assert rows()[-1]["session_id"] != first


def test_opt_out_and_global_privacy_control_stop_recording(monkeypatch):
    c, sig = setup(monkeypatch)
    assert c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}]},
                  headers={"Sec-GPC": "1"}).json() == {"stored": 0, "tracking": "off"}
    c.post("/settings", data={"alert_threshold": 8, "no_tracking": "on"})
    assert db.get_user(55)["tracking_opt_out"] == 1
    assert c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}]}).json()["stored"] == 0
    c.post(f"/feedback/{sig['id']}/1")
    assert rows() == []                                              # no behaviour recorded at all
    assert db.get().execute("SELECT useful FROM feedback").fetchone()["useful"] == 1   # explicit rating kept
    assert "not being recorded" in c.get("/dashboard").text


def test_rating_toggles_and_is_recorded(monkeypatch):
    c, sig = setup(monkeypatch)
    h = {"Accept": "application/json"}
    assert c.post(f"/feedback/{sig['id']}/1", headers=h).json() == {"state": 1}
    assert c.post(f"/feedback/{sig['id']}/0", headers=h).json() == {"state": 0}
    assert c.post(f"/feedback/{sig['id']}/0", headers=h).json() == {"state": None}   # tap again to clear
    assert [r["action"] for r in rows()] == ["like", "dislike", "like_cleared"]
    assert db.get().execute("SELECT COUNT(*) n FROM feedback").fetchone()["n"] == 0
    assert c.post(f"/feedback/{sig['id']}/7").status_code == 404


def test_watchlist_and_filter_events(monkeypatch):
    c, sig = setup(monkeypatch)
    c.get("/dashboard?type=strategic_investment")
    c.get("/dashboard?type=strategic_investment")   # same filter again: logged once
    c.get("/dashboard?type=not_a_type")             # ignored
    c.post("/watchlist/remove", data={"symbol": "EXMP.V"})
    got = [(r["action"], r["ticker"], r["event_type"]) for r in rows("watchlist_events")]
    assert got == [("add", "EXMP.V", None), ("filter", None, "strategic_investment"), ("remove", "EXMP.V", None)]
    assert "doesn" in c.post("/watchlist/add", data={"symbol": "<script>"}).text   # rejected symbol


def test_telegram_taps_use_a_daily_pseudonym(monkeypatch, fresh_db):
    c, sig = setup(monkeypatch)
    bot.handle_command({"id": 70, "first_name": "Te"}, 70, "/start")
    bot.handle_callback({"id": "cb", "data": f"fb:{sig['id']}:1", "from": {"id": 70}})
    bot.handle_callback({"id": "cb", "data": "fb:1:1:extra", "from": {"id": 70}})     # malformed: ignored
    bot.handle_callback({"id": "cb", "data": "fb:abc:1", "from": {"id": 70}})
    ev = rows()[-1]
    assert ev["surface"] == "telegram" and ev["action"] == "like"
    assert ev["session_id"] == behaviour.telegram_session_id(70) != behaviour.telegram_session_id(70, "2000-01-01")
    bot.handle_command({"id": 70}, 70, "/stop")
    assert db.get().execute("SELECT COUNT(*) n FROM behaviour_events WHERE surface='telegram'").fetchone()["n"] == 0


def test_engagement_by_event_type(monkeypatch):
    c, sig = setup(monkeypatch)
    for _ in range(2):  # reloading the page does not double-count
        c.post("/api/events", json={"events": [{"a": "impression", "s": sig["id"]}, {"a": "open", "s": sig["id"]},
                                                {"a": "dwell", "s": sig["id"], "v": 12}]})
    c.post(f"/feedback/{sig['id']}/1")
    m = behaviour.engagement_by_event_type()["strategic_investment"]
    assert m["impressions"] == 1 and m["opens"] == 1 and m["open_rate"] == 1.0
    assert m["avg_dwell"] == 24.0 and m["likes"] == 1 and m["like_rate"] == 1.0 and m["ctr"] == 0.0


def test_rate_limit_on_events(monkeypatch):
    c, sig = setup(monkeypatch)
    monkeypatch.setitem(security.limiter.limits, "events", (3, 60))
    codes = [c.post("/api/events", json={"events": [{"a": "open", "s": sig["id"]}]}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_comment_is_side_input_and_sanitised(monkeypatch):
    c, sig = setup(monkeypatch)
    c.post(f"/comments/{sig['id']}", data={"text": "Wrong\x00 investor <b>name</b>   here"})
    stored = db.get().execute("SELECT text FROM comments").fetchone()["text"]
    assert stored == "Wrong investor <b>name</b> here"            # kept as plain text; escaped when shown
    assert "Write a short comment" in c.post(f"/comments/{sig['id']}", data={"text": " "}).text


def test_privacy_page_is_public():
    from app.web import app
    body = TestClient(app).get("/privacy").text
    assert "anonymous session id" in body and "/tracking off" in body
