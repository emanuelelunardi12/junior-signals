"""Recent price move, used by the 'already moved' scoring rule.

Source: Stooq daily CSV (free, no key). A ticker uses `price_symbol` from universe.yaml, e.g. 'exmp.v'
for TSX-V names on Stooq. Tickers without one, or any failure, return None and the rule is simply skipped.
"""
import csv
import io

import httpx

from app import config, db

URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"


def _closes(symbol: str) -> list[float]:
    r = httpx.get(URL.format(symbol=symbol.lower()), timeout=20)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return [float(row["Close"]) for row in rows if row.get("Close") not in (None, "", "N/D")]


def recent_move_pct(ticker: str, days: int = 5) -> float | None:
    """Percentage change over the last `days` trading days, or None if unavailable."""
    if config.PRICE_SOURCE != "stooq":
        return None
    row = db.get().execute("SELECT price_symbol FROM tickers WHERE symbol=?", (ticker,)).fetchone()
    symbol = row["price_symbol"] if row else None
    if not symbol:
        return None
    try:
        closes = _closes(symbol)
    except Exception as exc:
        print(f"[prices] {ticker}: {exc}")
        return None
    if len(closes) < days + 1:
        return None
    first, last = closes[-(days + 1)], closes[-1]
    return None if first == 0 else round(100.0 * (last - first) / first, 1)
