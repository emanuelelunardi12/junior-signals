# Notes for Claude Code

Student venture MVP (ESADE, "Launching AI native ventures"). Retail traders of junior mining and oil & gas
stocks get early operational signals (strategic stakes, ownership thresholds, key hires, financings) via a
Telegram bot and a dashboard. Users sign up with Telegram only; the Telegram user id is the account id.

## Architecture
- `app/collectors.py` sources → `documents` table. RSS + SEC EDGAR. SEDAR+/SEDI are NOT scraped (terms); manual ingest.
- `app/ai.py` classify → extract (Maker, every field has a verbatim quote) → deterministic quote check → Grader → one retry.
- `app/scoring.py` transparent points; all weights in `WEIGHTS`/`THRESHOLDS`. Breakdown is shown to users.
- `app/pipeline.py` orchestration, dedupe, context (cluster, first-time holder), tiering.
- `app/publish.py` delivery modes per signal (`watchers`, `feed_only`, `broadcast`) and announcements; never sends twice.
- `/admin` in `app/web.py`: edit headline, reviewer score adjustment (stored as a breakdown line with reason), publish options, manual signal creation, announcements.
- `app/bot.py` Telegram commands and feedback buttons. `app/telegram.py` API client + login signature check.
- `app/web.py` FastAPI dashboard, admin review, webhook. Templates in `app/templates`, CSS in `app/static`.
- `app/digest.py` weekly summary per user; `app/retro.py` back-test over historical documents (never delivers).
- `app/prices.py` optional Stooq price move for the "already moved" rule; returns None when disabled or failing.
- `app/scheduler.py` optional APScheduler jobs (pipeline, digest) with failure messages to ADMIN_IDS.
- SQLite via plain sqlite3 (`app/db.py`). Deployment: `Dockerfile`, `render.yaml`, GitHub Actions test workflow.

## Rules to keep
- Never publish a signal without a source link; never phrase anything as buy/sell advice.
- Alert-tier (score >= 8) signals always go through human review in `/admin`.
- A field is kept only if its quote is found in the document AND the Grader supports it.
- Keep scoring rule-based and explainable; log every prompt/weight change in `docs/prompt_log.md`.
- Verify the Telegram login hash before creating a session. Check the webhook secret header.
- Tests must not call real APIs: fake `ai._call_tool` and `telegram.send_message` (see tests/conftest.py).
- Schema changes to existing tables go in `db.MIGRATIONS` so older databases upgrade.
- Run `python -m pytest -q` after changes.

## Backlog (in priority order)
1. Real tickers + feeds in universe.yaml; run the Handout 5 evaluation on ~50 real releases.
2. Deploy, connect the bot, onboard the five interviewees; run the retro study on 15 to 20 past deals.
3. Show the price move next to signals on the dashboard (the price source exists; only the display is missing).
4. Claim staking and permit registries for the first jurisdiction (v2 signals), with property-to-ticker linking.
5. Weekly sample check of documents classified as noise, with the result recorded (false-negative rate).
6. Telegram Mini App version of the dashboard.
7. Move from SQLite to Postgres when there are more than a few dozen users.
