"""Pipeline test with the AI calls replaced by fakes (no API key needed)."""
from app import ai, bot, collectors, db, pipeline

DOC = open("eval_data/docs/example_strategic.txt").read().split("\n", 1)[1]


def fake_calls(monkeypatch, bad_quote=False):
    def fake(stage, model, system, user, tool, max_tokens=2000):
        if stage == "classify":
            return {"event_type": "strategic_investment", "reason": "stake by producer"}
        if stage == "extract":
            items = [
                {"field": "headline", "value": "Big Copper buys 9.9% of Example Copper at a premium",
                 "quote": "Big Copper will hold approximately 9.9% of the issued and outstanding common shares"},
                {"field": "counterparty_name", "value": "Big Copper Mining Ltd.",
                 "quote": "subscription agreement with Big Copper Mining Ltd."},
                {"field": "counterparty_type", "value": "major_producer",
                 "quote": "subscription agreement with Big Copper Mining Ltd."},
                {"field": "price_vs_market", "value": "premium",
                 "quote": "represents a 25% premium to the 20-day volume weighted average price"},
                {"field": "board_seat", "value": True,
                 "quote": "right to nominate one director to the Company's board"},
                {"field": "stake_pct", "value": 19.9 if bad_quote else 9.9,
                 "quote": "hold approximately 19.9% of the shares" if bad_quote else "hold approximately 9.9% of the issued"},
            ]
            return {"items": items}
        if stage == "grade":
            return {"checks": [{"field": f, "supported": True} for f in
                               ["headline", "counterparty_name", "counterparty_type", "price_vs_market", "board_seat", "stake_pct"]]}
    monkeypatch.setattr(ai, "_call_tool", fake)


def test_quote_check_is_whitespace_and_quote_tolerant():
    assert ai.quote_in_doc("right to nominate one  director to the Company\u2019s board", DOC)
    assert not ai.quote_in_doc("right to nominate two directors", DOC)


def test_end_to_end_alert_publishes_automatically(monkeypatch, fresh_db):
    """The alert path has no human gate: an alert-tier document is live and on Telegram after one run."""
    fake_calls(monkeypatch)
    bot.handle_command({"id": 7, "first_name": "Ana"}, 7, "/start")
    bot.handle_command({"id": 7}, 7, "/add exmp.v")
    fresh_db.clear()
    collectors.save_document("EXMP.V", "manual", "https://example.com/1", "Strategic investment", None, DOC)
    summary = pipeline.run(collect=False)
    s = db.get().execute("SELECT * FROM signals").fetchone()
    assert s["tier"] == "alert" and s["status"] == "published" and s["published_at"]
    assert summary["published"] == 1
    assert [uid for uid, _ in fresh_db] == [7] and "EXMP.V" in fresh_db[0][1]
    assert "approximately 9.9%" in fresh_db[0][1]          # the verbatim quote travels with the alert
    from app import publish
    assert publish.go_live(s["id"]) == 0                   # never sent twice
    assert db.get().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"] == 1


def test_invented_quote_is_rejected(monkeypatch):
    fake_calls(monkeypatch, bad_quote=True)
    kept, rejected = ai.extract_verified(DOC, "", "strategic_investment")
    assert "stake_pct" not in {k["field"] for k in kept}
    assert any(r["field"] == "stake_pct" and "not found" in r["reason"] for r in rejected)


def test_duplicate_documents_make_one_signal(monkeypatch):
    fake_calls(monkeypatch)
    collectors.save_document("EXMP.V", "manual", "https://example.com/a", "t", None, DOC)
    collectors.save_document("EXMP.V", "rss", "https://example.com/b", "t", None, DOC)
    pipeline.run(collect=False)
    assert db.get().execute("SELECT COUNT(*) n FROM signals").fetchone()["n"] == 1
