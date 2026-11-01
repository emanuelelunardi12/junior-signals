"""Command line entry points. Usage: python -m app.cli <command> [options]"""
import argparse
import json
from pathlib import Path

from app import config, db, pipeline, telegram


def cmd_init(_):
    db.get()
    n = db.sync_universe()
    print(f"Database ready at {config.DATABASE_PATH}; {n} tickers loaded from universe.yaml")


def cmd_run(a):
    pipeline.run(collect=not a.no_collect)


def cmd_ingest(a):
    """Add a document by hand (e.g. from SEDAR+ or SEDI, which we don't scrape)."""
    from app import collectors
    db.sync_universe()
    if not db.ticker_exists(a.ticker):
        raise SystemExit(f"{a.ticker} is not in config/universe.yaml")
    text = Path(a.file).read_text(encoding="utf-8") if a.file else collectors.fetch_text(a.url)
    new = collectors.save_document(a.ticker.upper(), "manual", a.url, a.title or "", a.date, text)
    print("Saved. Run `python -m app.cli run --no-collect` to process it." if new else "Already stored (same URL).")


def cmd_digest(a):
    from app import digest
    sent = digest.send_weekly(days=a.days)
    print(f"Weekly digest sent to {sent} user(s)")


def cmd_retro(a):
    from app import retro
    db.sync_universe()
    retro.run(a.docs, a.out)


def cmd_set_webhook(_):
    url = f"{config.BASE_URL}/telegram/webhook"
    telegram.set_webhook(url)
    print(f"Webhook set to {url}")


def cmd_evaluate(a):
    from app import evaluate
    evaluate.main(a.docs, a.key)


def cmd_learn(a):
    """One learning cycle: aggregates behaviour and PROPOSES weight changes. Applies nothing."""
    from app import learning
    out = learning.run_cycle(window_days=a.days)
    print(f"Cycle {out['cycle_id']}: {out['events']} events, {len(out['proposals'])} proposal(s). "
          f"Review them at {config.BASE_URL}/admin/tuning; nothing changes until a founder approves.")


def cmd_insights(a):
    """AI insight job: suggestions only, shown at /admin/insights."""
    from app import insights
    row = insights.run(window_days=a.days)
    r = db.get().execute("SELECT status, note FROM ai_insights WHERE id=?", (row,)).fetchone()
    print(f"Insight run {row}: {r['status']} {r['note'] or ''}".strip())


def cmd_load_listings(a):
    """NASDAQ/NYSE from the SEC (download or a saved copy); TSX/TSXV from a CSV a founder saved by hand."""
    from app import listings
    db.get()
    if a.source == "sec":
        n = listings.load_sec(json.loads(Path(a.file).read_text(encoding="utf-8"))) if a.file else listings.fetch_sec()
    else:
        if not a.file or a.exchange not in ("TSX", "TSXV"):
            raise SystemExit("--source tmx needs --exchange TSX|TSXV and --file path.csv")
        n = listings.load_tmx_csv(Path(a.file).read_text(encoding="utf-8-sig"), a.exchange)
    print(f"Loaded {n} listings.")
    for row in listings.summary():
        print(f"  {row['exchange']:7} selectable {row['selectable']:6}  EDGAR-capable {row['edgar_capable']:6}  "
              f"newswire feeds {row['newswire_feeds']}")


def cmd_seed_demo(_):
    """Fake signals so the dashboard can be viewed without API keys. Never run against real data."""
    from app import demo
    n = demo.seed()
    if n:
        print(f"Added {n} demo signals. In the app, add EXMP.V and DEMO to your watchlist, or use the "
              "'Load demo data' button on the dashboard.")
    else:
        print("Demo signals were already there. Add EXMP.V and DEMO to your watchlist to see them.")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    r = sub.add_parser("run"); r.add_argument("--no-collect", action="store_true"); r.set_defaults(fn=cmd_run)
    i = sub.add_parser("ingest")
    i.add_argument("--ticker", required=True); i.add_argument("--url", required=True)
    i.add_argument("--file"); i.add_argument("--title"); i.add_argument("--date")
    i.set_defaults(fn=cmd_ingest)
    dg = sub.add_parser("digest"); dg.add_argument("--days", type=int, default=7); dg.set_defaults(fn=cmd_digest)
    rt = sub.add_parser("retro"); rt.add_argument("--docs", default="retro_data")
    rt.add_argument("--out", default="data/retro_timeline.csv"); rt.set_defaults(fn=cmd_retro)
    sub.add_parser("set-webhook").set_defaults(fn=cmd_set_webhook)
    e = sub.add_parser("evaluate"); e.add_argument("--docs", default="eval_data/docs")
    e.add_argument("--key", default="eval_data/answer_key.csv"); e.set_defaults(fn=cmd_evaluate)
    sub.add_parser("seed-demo").set_defaults(fn=cmd_seed_demo)
    ll = sub.add_parser("load-listings"); ll.add_argument("--source", choices=["sec", "tmx"], required=True)
    ll.add_argument("--exchange", type=str.upper); ll.add_argument("--file"); ll.set_defaults(fn=cmd_load_listings)
    ln = sub.add_parser("learn"); ln.add_argument("--days", type=int, default=30); ln.set_defaults(fn=cmd_learn)
    ins = sub.add_parser("insights"); ins.add_argument("--days", type=int, default=30); ins.set_defaults(fn=cmd_insights)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
