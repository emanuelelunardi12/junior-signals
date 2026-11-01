"""Reliability: several threads (requests, scheduler) using the database at once."""
import threading

from app import behaviour, config, db


def test_concurrent_writers_and_readers_on_a_file_database(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "signals.db")
    db.use(None)                                     # per-thread connections to a real file, as in production
    db.sync_universe()
    with db.tx() as c:
        d = c.execute("INSERT INTO documents (ticker, source, url, title, text, fetched_at, status) "
                      "VALUES ('EXMP.V','rss','https://x.y/1','t','t',?, 'extracted')", (db.now(),)).lastrowid
        sid = c.execute("INSERT INTO signals (document_id, ticker, event_type, headline, fields, score, breakdown, "
                        "tier, status, dedupe_key, created_at) VALUES (?, 'EXMP.V','private_placement','h','{}',1,"
                        "'[]','feed','published','k',?)", (d, db.now())).lastrowid
    errors = []

    def worker(n):
        try:
            for _ in range(60):
                batch = behaviour.parse_batch({"events": [{"a": "open", "s": sid}, {"a": "impression", "s": sid}]})
                behaviour.record_browser_events(f"session-{n:02d}-xxxxxxxxxxxxx", batch)
                behaviour.engagement_by_event_type()
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert db.get().execute("SELECT COUNT(*) n FROM behaviour_events").fetchone()["n"] == 6 * 60 * 2
    assert db.get().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
