"""Expanded ticker universe: TSX, TSX Venture, NASDAQ, NYSE, and honest coverage per listing."""
import httpx
from fastapi.testclient import TestClient

from app import collectors, config, db, listings

SEC = {"fields": ["cik", "name", "ticker", "exchange"], "data": [
    [1234567, "Example Gold Corp", "EXG", "NYSE"],
    [2345678, "Copper Hill Inc", "CPH", "Nasdaq"],
    [3456789, "Pink Sheet Minerals", "PSMX", "OTC"],          # out of scope
    [4567890, "Bad Symbol Co", "B@D", "Nasdaq"],               # invalid
    [5678901, "Oil Basin Partners", "OBP-A", "NYSE"],
]}

TMX_CSV = """TSX Venture Exchange listed issuers,,,
As at 31 October 2026,,,
Exchange,Name,Root Ticker,Sector
TSXV,Northern Copper Ltd,NCU,Mining
TSXV,Prairie Oil Corp,POC,Oil & Gas
TSXV,Northern Copper Ltd,NCU,Mining
"""


def test_sec_list_loads_nasdaq_and_nyse_only():
    assert listings.load_sec(SEC) == 3
    rows = {r["symbol"]: r for r in db.get().execute("SELECT * FROM listings WHERE source='sec'")}
    assert set(rows) == {"EXG", "CPH", "OBP-A"}
    assert rows["CPH"]["exchange"] == "NASDAQ" and rows["EXG"]["cik"] == "1234567"
    # a later file without a name marks it inactive instead of deleting it
    listings.load_sec({"fields": SEC["fields"], "data": SEC["data"][:2]})
    assert db.get().execute("SELECT status FROM listings WHERE symbol='OBP-A'").fetchone()["status"] == "inactive"
    assert listings.search("OBP") == []


def test_tmx_list_loaded_by_hand_gets_canadian_suffixes():
    assert listings.load_tmx_csv(TMX_CSV, "TSXV") == 2
    row = db.get().execute("SELECT * FROM listings WHERE symbol='NCU.V'").fetchone()
    assert row["exchange"] == "TSXV" and row["sector"] == "Mining" and row["source"] == "tmx_manual"
    assert listings.load_tmx_csv("Symbol;Issuer\nABX;Barrick Mining Corp\n", "TSX") == 1
    assert db.get().execute("SELECT name FROM listings WHERE symbol='ABX.TO'").fetchone()["name"] == "Barrick Mining Corp"
    try:
        listings.load_tmx_csv("a,b\n1,2\n", "TSX")
        raise AssertionError("expected a ValueError")
    except ValueError:
        pass


def test_search_ranks_symbols_first_and_escapes_wildcards():
    listings.load_sec(SEC)
    listings.load_tmx_csv(TMX_CSV, "TSXV")
    assert [r["symbol"] for r in listings.search("cph")][0] == "CPH"
    assert {r["symbol"] for r in listings.search("copper")} == {"CPH", "NCU.V", "EXMP.V"}
    assert listings.search("%") == [] and listings.search("_") == []
    assert listings.search("   ") == []


def test_coverage_is_reported_honestly():
    listings.load_sec(SEC)
    listings.load_tmx_csv(TMX_CSV, "TSXV")
    assert listings.coverage("EXG") == "EDGAR"                 # SEC filer: EDGAR collector applies
    assert listings.coverage("NCU.V") == "not covered yet"     # TSXV without a feed: selectable only
    with db.tx() as c:
        c.execute("""UPDATE tickers SET rss='["https://feeds.example.com/x"]' WHERE symbol='EXMP.V'""")
    assert listings.coverage("EXMP.V") == "newswire"
    summary = {r["exchange"]: r for r in listings.summary()}
    assert summary["NYSE"]["selectable"] == 3 and summary["NYSE"]["edgar_capable"] == 2   # + DEMO from universe.yaml
    assert summary["TSXV"]["selectable"] == 3 and summary["TSXV"]["newswire_feeds"] == 1


def test_watched_listing_is_wired_into_the_edgar_collector(monkeypatch):
    listings.load_sec(SEC)
    listings.load_tmx_csv(TMX_CSV, "TSXV")
    db.upsert_user(1, "A", None)
    assert db.add_to_watchlist(1, "EXG") is True
    assert "EXG" in {t["symbol"] for t in collectors.tickers_to_collect()}
    assert "CPH" not in {t["symbol"] for t in collectors.tickers_to_collect()}   # nobody watches it: no requests
    monkeypatch.setattr(collectors.time, "sleep", lambda s: None)
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "submissions" in url:
            return httpx.Response(200, json={"filings": {"recent": {
                "form": ["8-K", "10-Q"], "accessionNumber": ["0001-26-000001", "0001-26-000002"],
                "primaryDocument": ["ex.htm", "q.htm"], "filingDate": ["2026-10-30", "2026-10-29"]}}},
                request=httpx.Request("GET", url))
        return httpx.Response(200, text="<html><body>Strategic investment text</body></html>",
                              headers={"content-type": "text/html"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    t = db.get().execute("SELECT * FROM tickers WHERE symbol='EXG'").fetchone()
    assert collectors.collect_edgar(t) == 1
    assert "CIK0001234567" in calls[0]
    doc = db.get().execute("SELECT * FROM documents WHERE ticker='EXG'").fetchone()
    assert doc["source"] == "edgar" and doc["title"] == "8-K"


def test_adding_an_uncovered_listing_says_so_and_logs_demand(monkeypatch):
    listings.load_tmx_csv(TMX_CSV, "TSXV")
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": 31, "first_name": "T"})
    r = c.post("/watchlist/add", data={"symbol": "NCU.V"})
    assert "don&#39;t collect its news automatically yet" in r.text and "not covered yet" in r.text
    assert db.get().execute("SELECT symbol FROM ticker_requests").fetchone()["symbol"] == "NCU.V"
    assert "not on TSX, TSX Venture, NASDAQ or NYSE" in c.post("/watchlist/add", data={"symbol": "ZZZZ"}).text


def test_search_endpoint_and_dashboard_results(monkeypatch):
    listings.load_sec(SEC)
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    from app.web import app
    c = TestClient(app)
    assert c.get("/api/tickers?q=EX").status_code == 401
    c.post("/dev-login", data={"user_id": 32, "first_name": "T"})
    got = c.get("/api/tickers?q=gold").json()["results"]
    assert got == [{"symbol": "EXG", "exchange": "NYSE", "name": "Example Gold Corp", "coverage": "EDGAR"}]
    body = c.get("/dashboard?q=gold").text
    assert "Example Gold Corp" in body and 'value="EXG"' in body
    c.post("/watchlist/add", data={"symbol": "EXMP.V"})
    c.post("/watchlist/add", data={"symbol": "DEMO"})
    body = c.get("/dashboard").text            # regression: the search box must not show SQL placeholders
    assert 'name="q" value=""' in body and "No listing matches" not in body


def test_admin_loads_a_tmx_file(monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "ADMIN_IDS", {1})
    from app.web import app
    c = TestClient(app)
    c.post("/dev-login", data={"user_id": 2})
    assert c.post("/admin/listings", data={"source": "tmx", "exchange": "TSXV"},
                  files={"file": ("tsxv.csv", TMX_CSV.encode(), "text/csv")}).status_code == 403
    c.post("/dev-login", data={"user_id": 1})
    r = c.post("/admin/listings", data={"source": "tmx", "exchange": "TSXV"},
               files={"file": ("tsxv.csv", TMX_CSV.encode(), "text/csv")})
    assert "Loaded 2 TSXV listings" in r.text and "Ticker universe" in r.text


def test_universe_yaml_tickers_are_listings_too():
    row = db.get().execute("SELECT * FROM listings WHERE symbol='EXMP.V'").fetchone()
    assert row["exchange"] == "TSXV" and row["source"] == "universe_yaml"
    assert db.get().execute("SELECT exchange FROM listings WHERE symbol='DEMO'").fetchone()["exchange"] == "NYSE"
