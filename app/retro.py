"""Retro-signal study: run the pipeline over historical documents and export a timeline per ticker.

Tests our main guess: do these signals appear before the price moves or the deal is announced?

Put one text file per document in a folder, named TICKER__YYYY-MM-DD__anything.txt, e.g.
  EXMP.V__2024-03-11__strategic-stake.txt
Nothing is sent to users: signals are stored with their original date and marked status='retro'.
"""
import csv
import json
import re
from pathlib import Path

from app import collectors, db, pipeline

NAME = re.compile(r"^(?P<ticker>[^_]+)__(?P<date>\d{4}-\d{2}-\d{2})__(?P<slug>.+)\.txt$")


def ingest_folder(folder: Path) -> list[tuple]:
    """Saves each file as a document dated by its filename. Returns (document_row, date) pairs."""
    out = []
    for path in sorted(folder.glob("*.txt")):
        m = NAME.match(path.name)
        if not m:
            print(f"[retro] skipping {path.name}: expected TICKER__YYYY-MM-DD__slug.txt")
            continue
        ticker, date = m["ticker"].upper(), m["date"]
        if not db.ticker_exists(ticker):
            print(f"[retro] skipping {path.name}: {ticker} is not in universe.yaml")
            continue
        text = path.read_text(encoding="utf-8")
        title = ""
        if text.startswith("TITLE:"):
            first, _, text = text.partition("\n")
            title = first[6:].strip()
        url = f"retro://{path.name}"
        collectors.save_document(ticker, "retro", url, title or m["slug"], date, text)
        doc = db.get().execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
        if doc["status"] == "new":
            out.append((doc, date))
    return out


def run(folder: str | Path, out_csv: str | Path) -> dict:
    folder, out_csv = Path(folder), Path(out_csv)
    pairs = ingest_folder(folder)
    made = 0
    for doc, date in pairs:
        signal_id = pipeline.process_document(doc, deliver=False, skip_dedupe=True,
                                              created_at=f"{date}T00:00:00+00:00")
        if signal_id:
            made += 1
            with db.tx() as c:
                c.execute("UPDATE signals SET status='retro' WHERE id=?", (signal_id,))
    rows = db.get().execute(
        """SELECT s.ticker, s.created_at, s.event_type, s.score, s.tier, s.headline, s.fields, d.url
           FROM signals s JOIN documents d ON d.id=s.document_id
           WHERE s.status='retro' ORDER BY s.ticker, s.created_at""").fetchall()
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "date", "event_type", "score", "tier", "headline", "counterparty", "source"])
        for r in rows:
            fields = json.loads(r["fields"])
            w.writerow([r["ticker"], r["created_at"][:10], r["event_type"], r["score"], r["tier"],
                        r["headline"], fields.get("counterparty_name", ""), r["url"]])
    summary = {"documents": len(pairs), "signals": made, "rows_exported": len(rows), "csv": str(out_csv)}
    print(summary)
    print("Next step: add the announcement date of each deal to the CSV and count how many signals "
          "appeared at least two weeks before it.")
    return summary
