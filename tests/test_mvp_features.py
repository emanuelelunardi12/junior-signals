"""Tests for the digest, retro mode, prices, noise promotion and the tuning report."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import bot, collectors, config, db, digest, pipeline, prices, publish, retro
from tests.test_pipeline import DOC, fake_calls


def admin_client(monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", {1})
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": 1, "first_name": "Admin"})
    return c


def make_signal(monkeypatch, url="https://example.com/x"):
    fake_calls(monkeypatch)
    collectors.save_document("EXMP.V", "manual", url, "t", None, DOC)
    pipeline.run(collect=False)
    s = db.get().execute("SELECT * FROM signals ORDER BY id DESC").fetchone()
    with db.tx() as c:
        c.execute("UPDATE signals SET status='approved' WHERE id=?", (s["id"],))
    return db.get().execute("SELECT * FROM signals WHERE id=?", (s["id"],)).fetchone()


# ---------------- weekly digest ----------------

def test_digest_goes_only_to_users_with_matching_signals(monkeypatch, fresh_db):
    for uid, sym in [(20, "EXMP.V"), (21, "DEMO")]:
        bot.handle_command({"id": uid, "first_name": "U"}, uid, "/start")
        db.add_to_watchlist(uid, sym)
    make_signal(monkeypatch)
    fresh_db.clear()
    assert digest.send_weekly() == 1
    assert fresh_db[0][0] == 20 and "EXMP.V" in fresh_db[0][1]


def test_digest_skips_paused_users(monkeypatch, fresh_db):
    bot.handle_command({"id": 22, "first_name": "U"}, 22, "/start")
    db.add_to_watchlist(22, "EXMP.V")
    db.set_user(22, paused=1)
    make_signal(monkeypatch)
    fresh_db.clear()
    assert digest.send_weekly() == 0


# ---------------- retro mode ----------------

def test_retro_dates_signals_and_sends_nothing(monkeypatch, fresh_db, tmp_path):
    fake_calls(monkeypatch)
    bot.handle_command({"id": 23, "first_name": "U"}, 23, "/start")
    db.add_to_watchlist(23, "EXMP.V")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "EXMP.V__2024-03-11__stake.txt").write_text("TITLE: Stake\n" + DOC, encoding="utf-8")
    (folder / "NOPE__2024-03-11__x.txt").write_text(DOC, encoding="utf-8")   # unknown ticker: skipped
    (folder / "badname.txt").write_text(DOC, encoding="utf-8")               # wrong pattern: skipped
    fresh_db.clear()
    out = tmp_path / "timeline.csv"
    summary = retro.run(folder, out)
    assert summary["documents"] == 1 and summary["signals"] == 1
    assert fresh_db == []  # nothing is ever delivered in retro mode
    s = db.get().execute("SELECT * FROM signals").fetchone()
    assert s["status"] == "retro" and s["created_at"].startswith("2024-03-11")
    body = out.read_text(encoding="utf-8")
    assert "EXMP.V,2024-03-11" in body and "Big Copper Mining Ltd." in body


# ---------------- prices ----------------

CSV = "Date,Open,High,Low,Close,Volume\n" + "".join(
    f"2026-01-0{i},1,1,1,{1.0 + i * 0.1:.1f},100\n" for i in range(1, 8))


def test_recent_move_pct(monkeypatch):
    monkeypatch.setattr(config, "PRICE_SOURCE", "stooq")
    with db.tx() as c:
        c.execute("UPDATE tickers SET price_symbol='exmp.v' WHERE symbol='EXMP.V'")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text=CSV, request=httpx.Request("GET", "https://stooq.com")))
    assert prices.recent_move_pct("EXMP.V", days=5) == pytest.approx(41.7, abs=0.2)


def test_prices_disabled_or_missing_symbol(monkeypatch):
    assert prices.recent_move_pct("EXMP.V") is None            # PRICE_SOURCE defaults to none
    monkeypatch.setattr(config, "PRICE_SOURCE", "stooq")
    assert prices.recent_move_pct("DEMO") is None              # no price_symbol in the test universe


def test_price_failure_does_not_break_scoring(monkeypatch):
    monkeypatch.setattr(config, "PRICE_SOURCE", "stooq")
    with db.tx() as c:
        c.execute("UPDATE tickers SET price_symbol='exmp.v' WHERE symbol='EXMP.V'")

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", boom)
    assert make_signal(monkeypatch)["score"] > 0


# ---------------- noise promotion ----------------

def test_promote_document_the_ai_skipped(monkeypatch):
    def noise_then_fields(stage, model, system, user, tool, max_tokens=2000):
        if stage == "classify":
            return {"event_type": "noise", "reason": "looks routine"}
        return fake_calls_dispatch(stage, model, system, user, tool, max_tokens)

    fake_calls(monkeypatch)
    from app import ai
    fake_calls_dispatch = ai._call_tool
    monkeypatch.setattr(ai, "_call_tool", noise_then_fields)
    collectors.save_document("EXMP.V", "manual", "https://example.com/n", "t", None, DOC)
    pipeline.run(collect=False)
    doc = db.get().execute("SELECT * FROM documents").fetchone()
    assert doc["status"] == "noise"
    c = admin_client(monkeypatch)
    r = c.post(f"/admin/noise/{doc['id']}/promote", data={"event_type": "strategic_investment"})
    assert "Signal created" in r.text
    assert db.get().execute("SELECT status FROM signals").fetchone()["status"] == "pending_review"


# ---------------- tuning report ----------------

def test_tuning_report_counts_events_and_rules(monkeypatch):
    s = make_signal(monkeypatch)
    publish.publish_signal(s["id"])
    bot.handle_command({"id": 24, "first_name": "U"}, 24, "/start")
    bot.handle_callback({"id": "cb", "data": f"fb:{s['id']}:1", "from": {"id": 24}})
    body = admin_client(monkeypatch).get("/admin/tuning").text
    assert "Strategic investment" in body and "1 of 1 (100%)" in body
    assert json.loads(s["breakdown"])[0][0].split(":")[0] in body
