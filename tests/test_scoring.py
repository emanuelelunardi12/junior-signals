from app.scoring import Context, score_signal


def test_strategic_stake_scores_as_alert():
    f = {"counterparty_type": "major_producer", "price_vs_market": "premium", "board_seat": True,
         "counterparty_name": "Big Copper"}
    r = score_signal("strategic_investment", f, Context(first_time_holder=True))
    assert r.score == 5 + 1 + 2 + 2 + 1
    assert r.tier == "alert"


def test_routine_dilution_is_archived():
    r = score_signal("private_placement", {"price_vs_market": "discount", "full_warrants": True}, Context())
    assert r.score == 0 and r.tier == "archive"


def test_cluster_bonus_is_capped():
    r = score_signal("key_hire_ma", {}, Context(recent_signal_count=5))
    assert r.score == 3 + 4


def test_size_vs_market_cap_and_control_band():
    f = {"amount_musd": 9.0, "stake_pct": 19.5}
    r = score_signal("ownership_threshold", f, Context(market_cap_musd=45))
    labels = [l for l, _ in r.breakdown]
    assert any("market cap" in l for l in labels) and any("under 20%" in l for l in labels)
    assert r.score == 4 + 1 + 1


def test_unknown_event_type():
    assert score_signal("noise", {}, Context()).score == 0
