"""Collectors: turn sources into rows in the documents table.

Implemented: newswire RSS feeds, SEC EDGAR, manual ingestion (for SEDAR+, SEDI, company sites and
anything else we check by hand). SEDAR+ and SEDI have no public API and restrict automated access,
so we do NOT scrape them; paste those documents in with `python -m app.cli ingest`.
"""
import json
import time

import feedparser
import httpx
from bs4 import BeautifulSoup

from app import config, db

EDGAR_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
               "SCHEDULE 13G", "SCHEDULE 13G/A", "8-K", "6-K"}

HTTP_HEADERS = {"User-Agent": config.SEC_USER_AGENT}


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def fetch_text(url: str) -> str:
    r = httpx.get(url, headers=HTTP_HEADERS, timeout=30, follow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    return html_to_text(r.text) if "html" in ctype or r.text.lstrip().startswith("<") else r.text


def save_document(ticker: str, source: str, url: str, title: str, published_at: str | None, text: str) -> bool:
    """Insert unless the URL is already stored. Returns True if new."""
    with db.tx() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO documents (ticker, source, url, title, published_at, text, fetched_at)
               VALUES (?,?,?,?,?,?,?)""",
            (ticker, source, url, title, published_at, text, db.now()),
        )
        return cur.rowcount == 1


def _known(url: str) -> bool:
    return db.get().execute("SELECT 1 FROM documents WHERE url=?", (url,)).fetchone() is not None


def collect_rss(ticker_row) -> int:
    new = 0
    matches = [m.lower() for m in json.loads(ticker_row["match"])]
    for feed_url in json.loads(ticker_row["rss"]):
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            url = e.get("link")
            if not url or _known(url):
                continue
            blob = f"{e.get('title', '')} {e.get('summary', '')}".lower()
            if matches and not any(m in blob for m in matches):
                continue
            try:
                text = fetch_text(url)
            except Exception as exc:  # keep going; one bad page must not stop the run
                print(f"[rss] {ticker_row['symbol']}: could not fetch {url}: {exc}")
                continue
            new += save_document(ticker_row["symbol"], "rss", url, e.get("title", ""), e.get("published"), text)
            time.sleep(1)  # be polite to the sites
    return new


def collect_edgar(ticker_row, max_filings: int = 20) -> int:
    cik = ticker_row["cik"]
    if not cik:
        return 0
    cik10 = str(cik).zfill(10)
    r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik10}.json", headers=HTTP_HEADERS, timeout=30)
    r.raise_for_status()
    recent = r.json().get("filings", {}).get("recent", {})
    new = 0
    rows = zip(recent.get("form", []), recent.get("accessionNumber", []),
               recent.get("primaryDocument", []), recent.get("filingDate", []))
    for form, acc, doc, date in list(rows)[:max_filings]:
        if form not in EDGAR_FORMS:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        if _known(url):
            continue
        try:
            text = fetch_text(url)
        except Exception as exc:
            print(f"[edgar] {ticker_row['symbol']}: could not fetch {url}: {exc}")
            continue
        new += save_document(ticker_row["symbol"], "edgar", url, form, date, text)
        time.sleep(0.2)  # SEC fair-access limit is 10 requests/second
    return new


def collect_all() -> int:
    total = 0
    for t in db.get().execute("SELECT * FROM tickers ORDER BY symbol").fetchall():
        for fn in (collect_rss, collect_edgar):
            try:
                total += fn(t)
            except Exception as exc:
                print(f"[collect] {fn.__name__} failed for {t['symbol']}: {exc}")
    return total
