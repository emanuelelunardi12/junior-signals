"""Demo data so the dashboard can be seen without API keys or real sources. Development only.

The strategic-investment signal is built from a full fictional news release, so its quotes are real
quotes from that text, exactly as the pipeline would store them.
"""
import json

from app import db

RELEASE = """VANCOUVER, British Columbia - Example Copper Corp. (TSXV: EXMP) ("Example" or the "Company") is pleased to announce that it has entered into a subscription agreement with Big Copper Mining Ltd. ("Big Copper") for a non-brokered private placement of 25,000,000 common shares at a price of C$0.50 per share for gross proceeds of C$12.5 million.
The issue price represents a 25% premium to the 20-day volume weighted average price of the Company's shares.
Upon closing, Big Copper will hold approximately 9.9% of the issued and outstanding common shares of the Company on a non-diluted basis.
Big Copper will have the right to nominate one director to the Company's board for so long as it holds at least 5% of the outstanding shares.
This is a fictional example document."""

STRATEGIC_FIELDS = {
    "headline": ("Big Copper Mining buys a 9.9% stake in Example Copper at a 25% premium, with the right to nominate a director.",
                 "Upon closing, Big Copper will hold approximately 9.9% of the issued and outstanding common shares"),
    "counterparty_name": ("Big Copper Mining Ltd.", "subscription agreement with Big Copper Mining Ltd."),
    "stake_pct": (9.9, "Upon closing, Big Copper will hold approximately 9.9% of the issued and outstanding common shares"),
    "amount_millions": (12.5, "for gross proceeds of C$12.5 million"),
    "price_vs_market": ("premium", "The issue price represents a 25% premium to the 20-day volume weighted average price"),
    "board_seat": (True, "Big Copper will have the right to nominate one director to the Company's board"),
}

DEMO = [
    # ticker, event, headline, breakdown, tier, status, text, fields
    ("EXMP.V", "strategic_investment", STRATEGIC_FIELDS["headline"][0],
     [["Strategic investment", 5], ["Investor is a major producer", 1], ["Priced at a premium to market", 2],
      ["Investor gets board seat, top-up rights or standstill", 2], ["First time this investor appears", 1]],
     "alert", "approved", RELEASE, STRATEGIC_FIELDS),
    ("EXMP.V", "key_hire_ma", "Example Copper appointed a VP Corporate Development with prior M&A experience.",
     [["M&A-type hire", 3], ["1 other signal in the last 60 days", 2]], "feed", "approved", None, None),
    ("DEMO", "private_placement", "Demo Energy closed a C$4 million private placement of units with full warrants.",
     [["Private placement", 1], ["Discount with full warrants (routine dilution)", -1]], "archive", "approved", None, None),
    ("DEMO", "ownership_threshold", "A fund reported crossing 10% ownership of Demo Energy in an early warning report.",
     [["Ownership threshold crossed", 4], ["First time this investor appears", 1]], "feed", "pending_review", None, None),
]

DEMO_TICKERS = ["EXMP.V", "DEMO"]


def seed(user_id: int | None = None) -> int:
    """Adds the demo signals once (safe to repeat) and, if given, puts the demo tickers on that user's watchlist.
    Returns the number of signals added this time."""
    db.sync_universe()
    added = 0
    with db.tx() as c:
        for i, (ticker, event, headline, breakdown, tier, status, text, fields) in enumerate(DEMO):
            if not c.execute("SELECT 1 FROM tickers WHERE symbol=?", (ticker,)).fetchone():
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO documents (ticker, source, url, title, text, fetched_at, event_type, status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ticker, "manual", f"https://example.com/demo/{i}", headline, text or headline, db.now(), event, "extracted"))
            if not cur.rowcount:
                continue
            if fields:
                stored = {k: v for k, (v, _) in fields.items()}
                stored["_quotes"] = {k: q for k, (_, q) in fields.items()}
            else:
                stored = {"headline": headline}
            c.execute(
                "INSERT INTO signals (document_id, ticker, event_type, headline, fields, score, breakdown, tier, status, "
                "dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, ticker, event, headline, json.dumps(stored), sum(p for _, p in breakdown),
                 json.dumps(breakdown), tier, status, f"demo{i}", db.now()))
            added += 1
    if user_id is not None:
        for t in DEMO_TICKERS:
            if db.ticker_exists(t):
                db.add_to_watchlist(user_id, t)
    return added
