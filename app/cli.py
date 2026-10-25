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
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
