"""The automatic alert path and the two red lines (no human in this path; see app/publish.py)."""
import json

from app import ai, bot, collectors, db, guardrails, pipeline, publish
from tests.test_pipeline import DOC, fake_calls


def watcher(uid=7, ticker="EXMP.V"):
    bot.handle_command({"id": uid, "first_name": "Ana"}, uid, "/start")
    db.add_to_watchlist(uid, ticker)


def run_doc(url="https://example.com/1", text=DOC):
    collectors.save_document("EXMP.V", "rss", url, "Strategic investment", None, text)
    pipeline.run(collect=False)
    return db.get().execute("SELECT * FROM signals ORDER BY id DESC").fetchone()


def test_alert_needs_no_approval_and_carries_source_quote_and_disclaimer(monkeypatch, fresh_db):
    fake_calls(monkeypatch)
    watcher()
    fresh_db.clear()
    s = run_doc()
    assert s["status"] == "published" and s["tier"] == "alert"
    text = fresh_db[0][1]
    assert 'href="https://example.com/1"' in text and "Source (example.com)" in text
    assert "“Big Copper will hold approximately 9.9%" in text
    assert "not investment advice" in text


def test_signal_without_verbatim_quote_is_withheld_automatically(monkeypatch, fresh_db):
    def no_good_quotes(stage, model, system, user, tool, max_tokens=2000):
        if stage == "classify":
            return {"event_type": "strategic_investment", "reason": "x"}
        if stage == "extract":
            return {"items": [{"field": "headline", "value": "Big Copper takes a stake",
                               "quote": "this sentence is not in the document at all"}]}
        return {"checks": []}

    monkeypatch.setattr(ai, "_call_tool", no_good_quotes)
    watcher()
    fresh_db.clear()
    s = run_doc()
    assert s["status"] == "withheld" and "verbatim quote" in s["status_note"]
    assert fresh_db == []                                   # nothing sent
    assert db.get().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"] == 0


def test_document_without_web_source_is_never_published(monkeypatch, fresh_db):
    fake_calls(monkeypatch)
    watcher()
    fresh_db.clear()
    s = run_doc(url="ftp://files.example.com/release.txt")
    assert s["status"] == "withheld" and "primary-source link" in s["status_note"]
    assert fresh_db == []


def test_advice_headline_is_replaced_and_the_alert_still_goes_out(monkeypatch, fresh_db):
    fake_calls(monkeypatch)
    real = ai._call_tool

    def advice_headline(stage, model, system, user, tool, max_tokens=2000):
        out = real(stage, model, system, user, tool, max_tokens)
        if stage == "extract":
            out["items"][0]["value"] = "Strong buy: Big Copper takes 9.9%, shares will soar"
        return out

    monkeypatch.setattr(ai, "_call_tool", advice_headline)
    watcher()
    fresh_db.clear()
    s = run_doc()
    assert s["status"] == "published"
    assert "buy" not in s["headline"].lower() and "soar" not in s["headline"].lower()
    assert s["headline"] == "Strategic investment in EXMP.V involving Big Copper Mining Ltd."
    assert any("investment advice" in r["reason"] for r in json.loads(s["rejected"]))
    assert len(fresh_db) == 1


def test_advice_detector_leaves_transaction_language_alone():
    for ok in ["Big Copper buys a 9.9% stake in Example Copper", "C$12.5 million bought deal financing",
               "Insider sells 2% of holdings", "Appointment of VP Corporate Development"]:
        assert guardrails.advice_phrase(ok) is None, ok
    for ok in ["Fund agreed to sell shares to Big Copper", "Big Copper to buy 9.9% of Example Copper",
               "Buyer of the royalty is Sandstorm", "Holder crosses 10% threshold after buying shares"]:
        assert guardrails.advice_phrase(ok) is None, ok
    for bad in ["Time to buy EXMP", "We recommend investors add", "price target of $2", "you should sell",
                "Strong buy rating", "shares will soar after this", "Analysts rate EXMP a Strong Buy", "Buy EXMP",
                "Accumulate EXMP before the drill results", "This is a buying opportunity", "Upgrade to buy",
                "Outperform rating reiterated"]:
        assert guardrails.advice_phrase(bad), bad


def test_founder_draft_is_still_checked_against_red_lines(monkeypatch, fresh_db):
    from tests.test_admin import admin_client, make_pending
    s = make_pending(monkeypatch)
    c = admin_client(monkeypatch)
    c.post(f"/admin/signal/{s['id']}/edit", data={"headline": "Time to buy Example Copper", "adjustment": 0})
    r = c.post(f"/admin/signal/{s['id']}/send")
    assert "Not published" in r.text and "investment advice" in r.text
    assert db.get().execute("SELECT status FROM signals").fetchone()["status"] == "withheld"


def test_retract_sends_a_correction_to_exactly_the_recipients(monkeypatch, fresh_db):
    fake_calls(monkeypatch)
    watcher(7)
    bot.handle_command({"id": 8, "first_name": "Bo"}, 8, "/start")   # not watching: never got it
    s = run_doc()
    fresh_db.clear()
    from tests.test_admin import admin_client
    c = admin_client(monkeypatch)
    assert "Give a reason" in c.post(f"/admin/signal/{s['id']}/retract", data={"reason": " "}).text
    r = c.post(f"/admin/signal/{s['id']}/retract", data={"reason": "Wrong company in the release"})
    assert "Correction sent to 1 user" in r.text
    assert [uid for uid, _ in fresh_db] == [7] and "Correction" in fresh_db[0][1]
    assert db.get().execute("SELECT status FROM signals").fetchone()["status"] == "retracted"
    assert publish.go_live(s["id"]) == 0                     # a retracted signal cannot come back by accident


def test_signal_left_queued_by_a_crash_is_published_on_the_next_run(monkeypatch, fresh_db):
    fake_calls(monkeypatch)
    watcher()
    real_go_live = publish.go_live
    monkeypatch.setattr(publish, "go_live", lambda sid: 0)   # simulate a crash right after scoring
    s = run_doc()
    assert s["status"] == "queued"
    monkeypatch.setattr(publish, "go_live", real_go_live)
    fresh_db.clear()
    pipeline.run(collect=False)
    assert db.get().execute("SELECT status FROM signals").fetchone()["status"] == "published"
    assert len(fresh_db) == 1


def test_pipeline_never_creates_drafts():
    """Guard against the review gate creeping back: only draft=True (hand-made in /admin) makes a draft."""
    import inspect
    src = inspect.getsource(pipeline.process_document)
    assert 'status = "draft" if draft else' in src
    assert "pending_review" not in inspect.getsource(pipeline) + inspect.getsource(publish)


def test_a_telegram_network_error_does_not_stop_delivery(monkeypatch, fresh_db):
    """One unreachable user (or a Telegram outage) must not abort the run or skip the remaining recipients."""
    import httpx
    from app import config, telegram
    fake_calls(monkeypatch)
    watcher(7)
    watcher(8)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "1:abc")
    sent = []

    def flaky_post(url, json=None, timeout=None):
        if json["chat_id"] == 7:
            raise httpx.ConnectError("network down")
        sent.append(json["chat_id"])
        return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", flaky_post)
    monkeypatch.setattr(telegram, "send_message", _real_send(telegram))   # undo the test fake: use the HTTP path
    s = run_doc()
    assert s["status"] == "published" and sent == [8]
    assert db.get().execute("SELECT status FROM documents").fetchone()["status"] == "extracted"


def _real_send(telegram):
    """Same as telegram.send_message, which the test fixture replaces with a fake."""
    def send(chat_id, text, buttons=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        return telegram.call("sendMessage", **params)
    return send
