"""collect -> classify -> extract (Maker) -> grade (Grader) -> score -> review / publish."""
import json
from datetime import datetime, timedelta, timezone

from app import ai, collectors, db, prices, publish, scoring

# Approximate FX to convert deal sizes for the market-cap rule. Update occasionally; precision is not critical.
APPROX_FX_TO_USD = {"USD": 1.0, "CAD": 0.72, "AUD": 0.65, "GBP": 1.3, "EUR": 1.1}

NUMERIC = {"amount_millions", "stake_pct"}
BOOLEAN = {"full_warrants", "board_seat", "topup_rights", "standstill"}


def _to_number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("$", "").strip())
    except ValueError:
        return None


def _to_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "yes", "1"}


def flatten(items: list[dict]) -> dict:
    out = {}
    for it in items:
        f, v = it["field"], it["value"]
        if f in NUMERIC:
            v = _to_number(v)
        elif f in BOOLEAN:
            v = _to_bool(v)
        elif isinstance(v, str):
            v = v.strip()
        if v is not None:
            out[f] = v
    cur = str(out.get("currency", "")).upper()
    if "amount_millions" in out and cur in APPROX_FX_TO_USD:
        out["amount_musd"] = out["amount_millions"] * APPROX_FX_TO_USD[cur]
    return out


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def build_context(ticker: str, f: dict) -> scoring.Context:
    c = db.get()
    t = c.execute("SELECT market_cap_musd FROM tickers WHERE symbol=?", (ticker,)).fetchone()
    recent = c.execute(
        """SELECT COUNT(*) AS n FROM signals WHERE ticker=? AND status!='rejected' AND tier!='archive'
           AND created_at >= ?""",
        (ticker, _since(scoring.THRESHOLDS["cluster_days"])),
    ).fetchone()["n"]
    first_time = False
    name = str(f.get("counterparty_name", "")).lower()
    if name:
        seen = c.execute("SELECT fields FROM signals WHERE ticker=?", (ticker,)).fetchall()
        first_time = not any(json.loads(r["fields"]).get("counterparty_name", "").lower() == name for r in seen)
    return scoring.Context(market_cap_musd=t["market_cap_musd"] if t else None,
                           first_time_holder=first_time, recent_signal_count=recent,
                           prior_move_pct=prices.recent_move_pct(ticker))


def dedupe_key(ticker: str, event_type: str, f: dict) -> str:
    who = str(f.get("counterparty_name") or f.get("person_name") or "").lower().strip()
    amt = f.get("amount_millions")
    return f"{ticker}|{event_type}|{who}|{amt if amt is not None else ''}"


def is_duplicate(key: str, days: int = 30) -> bool:
    return db.get().execute("SELECT 1 FROM signals WHERE dedupe_key=? AND created_at >= ?",
                            (key, _since(days))).fetchone() is not None


def process_document(doc, event_type: str | None = None, force_review: bool = False,
                     skip_dedupe: bool = False, deliver: bool = True, created_at: str | None = None) -> int | None:
    """Returns the new signal id, or None if the document was noise/duplicate.

    event_type: set by a reviewer to skip AI classification.
    force_review: put the signal in the review queue whatever its tier (used for signals created in /admin).
    deliver: set False for back-testing, so nothing is sent to users.
    created_at: override the timestamp, so historical documents are scored and stored with their own date.
    """
    text, title = doc["text"] or "", doc["title"] or ""
    if event_type not in scoring.EVENT_TYPES:
        event_type = ai.classify(text, title)
    if event_type == "noise":
        with db.tx() as c:
            c.execute("UPDATE documents SET event_type='noise', status='noise' WHERE id=?", (doc["id"],))
        return None

    kept, rejected = ai.extract_verified(text, title, event_type)
    f = flatten(kept)
    key = dedupe_key(doc["ticker"], event_type, f)
    if not skip_dedupe and is_duplicate(key):
        with db.tx() as c:
            c.execute("UPDATE documents SET event_type=?, status='duplicate' WHERE id=?", (event_type, doc["id"]))
        return None

    result = scoring.score_signal(event_type, f, build_context(doc["ticker"], f))
    status = "pending_review" if (result.tier == "alert" or force_review) else "approved"
    headline = f.get("headline") or title or scoring.EVENT_TYPES[event_type]
    fields_json = {it["field"]: it["value"] for it in kept}
    fields_json["_quotes"] = {it["field"]: it["quote"] for it in kept}
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO signals (document_id, ticker, event_type, headline, fields, rejected, score, breakdown,
               tier, status, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc["id"], doc["ticker"], event_type, headline, json.dumps(fields_json), json.dumps(rejected),
             result.score, json.dumps(result.breakdown), result.tier, status, key, created_at or db.now()),
        )
        c.execute("UPDATE documents SET event_type=?, status='extracted' WHERE id=?", (event_type, doc["id"]))
        signal_id = cur.lastrowid
    if status == "approved" and deliver:
        publish.publish_signal(signal_id)
    return signal_id


def run(collect: bool = True) -> dict:
    db.sync_universe()
    new_docs = collectors.collect_all() if collect else 0
    docs = db.get().execute("SELECT * FROM documents WHERE status='new' ORDER BY id").fetchall()
    made, failed = 0, 0
    for d in docs:
        try:
            if process_document(d):
                made += 1
        except Exception as exc:
            failed += 1
            print(f"[pipeline] document {d['id']} failed: {exc}")
            with db.tx() as c:
                c.execute("UPDATE documents SET status='failed' WHERE id=?", (d["id"],))
    summary = {"new_documents": new_docs, "processed": len(docs), "signals": made, "failed": failed}
    print(summary)
    return summary
