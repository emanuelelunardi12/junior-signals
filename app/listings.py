"""The ticker universe: every listing a user can find and add, and what we actually cover for each.

Exchanges (for now): TSX, TSX Venture, NASDAQ, NYSE.

Sources, and why:
- NASDAQ and NYSE: the SEC's company_tickers_exchange.json (https://www.sec.gov/files/company_tickers_exchange.json).
  US government data, free to use and redistribute; it also gives each company's CIK, which wires the name
  straight into our EDGAR collector. Fetched automatically (SEC fair-access rules: descriptive User-Agent,
  well under 10 requests/second). OTC and Cboe rows in the file are ignored for now.
- TSX and TSX Venture: TMX publishes its issuer lists, but its terms forbid copying or redistributing them
  without written consent and we found no open, redistributable alternative. So we do NOT scrape or bundle
  them: a founder downloads the list by hand and loads it (`python -m app.cli load-listings --source tmx` or the
  upload form in /admin). Before showing that list to many users we should ask TMX for permission.
- config/universe.yaml: our hand-curated tickers with their newswire feeds.

Coverage is not the same as being selectable. A listing is:
- "newswire + EDGAR", "newswire" or "EDGAR"   real automatic event coverage for that ticker;
- "not covered yet"                             selectable, but only signals a founder adds by hand will appear.
US names from the SEC list have a CIK, so EDGAR coverage (13D/13G, 8-K, 6-K) starts once someone watches them.
TSX/TSXV names are covered only if we configure a newswire feed in universe.yaml (SEDAR+ is never scraped).
"""
import csv
import io
import json
import re

from app import config, db

SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
EXCHANGES = ("TSX", "TSXV", "NASDAQ", "NYSE")
SUFFIX = {"TSX": ".TO", "TSXV": ".V"}          # same convention as our existing tickers (EXMP.V)
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _sec_exchange(raw) -> str | None:
    raw = str(raw or "").strip().upper()
    if raw == "NASDAQ":
        return "NASDAQ"
    if raw.startswith("NYSE"):
        return "NYSE"
    return None   # OTC, CBOE, blank: out of scope for now


def _upsert(rows: list[tuple], source: str, replace_source: bool) -> int:
    """rows: (symbol, exchange, name, cik, sector). replace_source marks rows of this source not in `rows` inactive."""
    now = db.now()
    with db.tx() as c:
        if replace_source:
            exchanges = sorted({r[1] for r in rows})
            if exchanges:
                q = ",".join("?" * len(exchanges))
                c.execute(f"UPDATE listings SET status='inactive' WHERE source=? AND exchange IN ({q})",
                          [source] + exchanges)
        c.executemany(
            """INSERT INTO listings (symbol, exchange, name, cik, sector, status, source, loaded_at)
               VALUES (?,?,?,?,?,'active',?,?)
               ON CONFLICT(symbol) DO UPDATE SET exchange=excluded.exchange, name=excluded.name,
               cik=COALESCE(excluded.cik, listings.cik), sector=COALESCE(excluded.sector, listings.sector),
               status='active', source=excluded.source, loaded_at=excluded.loaded_at""",
            [(s, e, n, k, sec, source, now) for s, e, n, k, sec in rows])
    return len(rows)


def load_sec(data: dict) -> int:
    """Load NASDAQ and NYSE listings from the SEC file's JSON ({"fields": [...], "data": [[...]]})."""
    fields = [f.lower() for f in data.get("fields", [])]
    idx = {f: fields.index(f) for f in ("cik", "name", "ticker", "exchange") if f in fields}
    if len(idx) < 4:
        raise ValueError("Unexpected SEC file format: need cik, name, ticker and exchange fields")
    rows, seen = [], set()
    for rec in data.get("data", []):
        ex = _sec_exchange(rec[idx["exchange"]])
        sym = str(rec[idx["ticker"]] or "").strip().upper()
        if not ex or not SYMBOL.match(sym) or sym in seen:
            continue
        seen.add(sym)
        rows.append((sym, ex, str(rec[idx["name"]] or "").strip()[:200] or sym, str(rec[idx["cik"]]), None))
    return _upsert(rows, "sec", replace_source=True)


def fetch_sec() -> int:
    """Download and load the SEC list. Returns rows loaded; 0 (with a log line) if the download fails."""
    import httpx
    try:
        r = httpx.get(SEC_URL, headers={"User-Agent": config.SEC_USER_AGENT}, timeout=60)
        r.raise_for_status()
        return load_sec(r.json())
    except Exception as exc:
        print(f"[listings] SEC download failed, keeping what we have: {exc}")
        return 0


def load_tmx_csv(text: str, exchange: str) -> int:
    """Load a TSX or TSXV issuer list a founder downloaded by hand and saved as CSV.
    Needs a symbol/ticker column and a name/issuer column; a sector column is kept if present."""
    exchange = exchange.upper()
    if exchange not in SUFFIX:
        raise ValueError("exchange must be TSX or TSXV")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header, sym_i, name_i, sec_i = None, None, None, None
    rows, seen = [], set()
    for line in reader:
        cells = [c.strip() for c in line]
        if header is None:
            low = [c.lower() for c in cells]
            sym_i = next((i for i, c in enumerate(low) if c in ("symbol", "ticker", "root ticker", "root_ticker")), None)
            name_i = next((i for i, c in enumerate(low) if c in ("name", "issuer", "company", "issuer name",
                                                                  "company name", "name of company")), None)
            if sym_i is not None and name_i is not None:
                header = low
                sec_i = next((i for i, c in enumerate(low) if c == "sector"), None)
            continue                                   # skip title rows above the header
        if len(cells) <= max(sym_i, name_i):
            continue
        root = cells[sym_i].upper().replace(" ", ".")
        root = root.removesuffix(SUFFIX[exchange])
        sym = root + SUFFIX[exchange]
        if not root or not SYMBOL.match(sym) or sym in seen:
            continue
        seen.add(sym)
        sector = cells[sec_i][:60] if sec_i is not None and sec_i < len(cells) and cells[sec_i] else None
        rows.append((sym, exchange, cells[name_i][:200] or sym, None, sector))
    if header is None:
        raise ValueError("No header row with a symbol/ticker column and a name/issuer column")
    return _upsert(rows, "tmx_manual", replace_source=True)


def normalise_exchange(raw: str | None) -> str:
    raw = re.sub(r"[\s_-]+", " ", (raw or "").strip().upper())
    if raw in ("TSXV", "TSX V", "TSX VENTURE", "TSX VENTURE EXCHANGE", "CVE"):
        return "TSXV"
    if raw in ("TSX", "TORONTO STOCK EXCHANGE", "TSE"):
        return "TSX"
    if raw.startswith("NYSE"):
        return "NYSE"
    if raw.startswith("NASDAQ"):
        return "NASDAQ"
    return raw   # e.g. CSE, ASX: kept as written, outside the four bulk-loaded exchanges


def sync_from_universe() -> None:
    """Curated tickers from universe.yaml are listings too (they may be on exchanges we don't bulk-load)."""
    rows = [(r["symbol"], normalise_exchange(r["exchange"]), r["name"], r["cik"], None)
            for r in db.get().execute("SELECT * FROM tickers WHERE configured=1")]
    with db.tx() as c:
        for sym, ex, name, cik, _ in rows:
            c.execute("""INSERT INTO listings (symbol, exchange, name, cik, status, source, loaded_at)
                         VALUES (?,?,?,?,'active','universe_yaml',?)
                         ON CONFLICT(symbol) DO UPDATE SET cik=COALESCE(listings.cik, excluded.cik)""",
                      (sym, ex, name, cik, db.now()))


# ---------------- coverage ----------------

def coverage(symbol: str) -> str:
    """'newswire + EDGAR' | 'newswire' | 'EDGAR' | 'not covered yet'."""
    c = db.get()
    t = c.execute("SELECT rss, cik FROM tickers WHERE symbol=?", (symbol,)).fetchone()
    cik = t["cik"] if t else None
    if not cik:
        row = c.execute("SELECT cik FROM listings WHERE symbol=?", (symbol,)).fetchone()
        cik = row["cik"] if row else None
    rss = bool(t and json.loads(t["rss"] or "[]"))
    if rss and cik:
        return "newswire + EDGAR"
    if rss:
        return "newswire"
    if cik:
        return "EDGAR"
    return "not covered yet"


def ensure_ticker(symbol: str) -> bool:
    """Make sure a listing a user picked exists in `tickers`, so watchlists and collectors can use it."""
    symbol = symbol.upper()
    c = db.get()
    if c.execute("SELECT 1 FROM tickers WHERE symbol=?", (symbol,)).fetchone():
        return True
    lst = c.execute("SELECT * FROM listings WHERE symbol=? AND status='active'", (symbol,)).fetchone()
    if lst is None:
        return False
    with db.tx() as c:
        c.execute("""INSERT OR IGNORE INTO tickers (symbol, name, exchange, cik, rss, match, configured)
                     VALUES (?,?,?,?, '[]', '[]', 0)""", (symbol, lst["name"], lst["exchange"], lst["cik"]))
    return True


def search(q: str, limit: int = 20) -> list[dict]:
    """Symbol prefix first, then name contains. q is plain text; LIKE wildcards are escaped."""
    q = re.sub(r"\s+", " ", (q or "").strip())[:40]
    if not q:
        return []
    esc = q.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.get().execute(
        """SELECT symbol, exchange, name, cik, sector,
                  CASE WHEN symbol = ? THEN 0 WHEN symbol LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END AS rank
           FROM listings WHERE status='active' AND (symbol LIKE ? ESCAPE '\\' OR UPPER(name) LIKE ? ESCAPE '\\')
           ORDER BY rank, LENGTH(symbol), symbol LIMIT ?""",
        (q.upper(), esc + "%", esc + "%", "%" + esc + "%", limit)).fetchall()
    return [dict(r, coverage=coverage(r["symbol"])) for r in rows]


def summary() -> list[dict]:
    """Per exchange: how many listings are selectable and how many have automatic event coverage."""
    out = []
    for ex in EXCHANGES:
        total = db.get().execute("SELECT COUNT(*) n FROM listings WHERE exchange=? AND status='active'", (ex,)).fetchone()["n"]
        with_cik = db.get().execute("SELECT COUNT(*) n FROM listings WHERE exchange=? AND status='active' AND cik IS NOT NULL",
                                    (ex,)).fetchone()["n"]
        feeds = sum(1 for r in db.get().execute("SELECT exchange, rss FROM tickers")
                    if normalise_exchange(r["exchange"]) == ex and json.loads(r["rss"] or "[]"))
        out.append({"exchange": ex, "selectable": total, "edgar_capable": with_cik, "newswire_feeds": feeds})
    return out
