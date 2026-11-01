# Notes for Claude Code

Student venture MVP (ESADE, "Launching AI native ventures"). Retail traders of junior mining and oil & gas
stocks get early operational signals (strategic stakes, ownership thresholds, key hires, financings) via a
Telegram bot and a dashboard. Users sign up with Telegram only; the Telegram user id is the account id.

## Architecture
- `app/collectors.py` sources → `documents` table. RSS + SEC EDGAR. SEDAR+/SEDI are NOT scraped (terms); manual ingest.
- `app/ai.py` classify → extract (Maker, every field has a verbatim quote) → deterministic quote check → Grader → one retry.
- `app/scoring.py` transparent points; all weights in `WEIGHTS`/`THRESHOLDS`. Breakdown is shown to users.
- `app/pipeline.py` orchestration, dedupe, context (cluster, first-time holder), tiering.
- `app/publish.py` `go_live()`: AUTOMATIC publishing (no human gate) after the red-line checks in `app/guardrails.py`;
  delivery modes (`watchers`, `feed_only`, `broadcast`); `deliveries` table so nothing is sent twice; `retract()` sends corrections.
- `/admin` in `app/web.py`: live feed with retract, withheld list, founder drafts (only hand-made signals), manual signal creation, announcements.
- `app/behaviour.py` behaviour events (daily pseudonymous ids, opt-out, retention) + per-event-type engagement;
  `app/static/track.js` sends impressions/opens/dwell/scroll-past/source clicks to `POST /api/events`.
- `app/learning.py` learning loop: `run_cycle()` PROPOSES ±1 weight changes from behaviour; only founder actions
  in `/admin/tuning` change a weight: `approve()` and `revert()`; `weight_changes` = versioned, append-only log.
- `app/insights.py` weekly AI insight job, numbers-only input, cited numbers verified in code, suggestion-only.
- `app/listings.py` ticker universe (SEC list for NASDAQ/NYSE; TMX lists loaded by hand) and coverage labels.
- `app/security.py` rate limits, security headers/CSP, same-origin check. `app/guardrails.py` the red lines.
- `app/bot.py` Telegram commands and feedback buttons. `app/telegram.py` API client + login signature check.
- `app/web.py` FastAPI dashboard, admin review, webhook. Templates in `app/templates`, CSS in `app/static`.
- `app/digest.py` weekly summary per user; `app/retro.py` back-test over historical documents (never delivers).
- `app/prices.py` optional Stooq price move for the "already moved" rule; returns None when disabled or failing.
- `app/scheduler.py` optional APScheduler jobs (pipeline, digest) with failure messages to ADMIN_IDS.
- SQLite via plain sqlite3 (`app/db.py`). Deployment: `Dockerfile`, `render.yaml`, GitHub Actions test workflow.

## Rules to keep
- RED LINES (enforced in `guardrails.py`, checked in `publish.go_live`): every published signal has an http(s) source link
  and a verbatim quote found in the document; nothing reads as buy/sell advice. Failing signals are withheld automatically.
- The alert path is automatic by design: never add a human approval step between scoring and publishing.
  The human sits in the LEARNING path: scoring-weight changes need founder approval in `/admin/tuning`.
- A field is kept only if its quote is found in the document AND the Grader supports it.
- Keep scoring rule-based and explainable; log every prompt/weight change in `docs/prompt_log.md`.
- Verify the Telegram login hash before creating a session. Check the webhook secret header.
- Never auto-apply a learning proposal; never send comments, session ids or tickers to the AI insight job.
- Behaviour events never store the Telegram user id; event type and ticker come from the server, not the browser.
- No inline `<script>` or `on*=` handlers (the CSP forbids them): put JS in `app/static/`.
- One SQLite connection per thread (`db.get()`); write through `db.tx()`.
- Tests must not call real APIs: fake `ai._call_tool` and `telegram.send_message` (see tests/conftest.py).
- Schema changes to existing tables go in `db.MIGRATIONS` so older databases upgrade.
- Run `python -m pytest -q` after changes.

## Backlog (in priority order)
1. Deploy on Render (needs a founder: plan confirmation + secrets), connect the bot, onboard the five interviewees.
2. Real tickers + newswire feeds in universe.yaml (TSX/TSXV); load the TMX list by hand; run `cli evaluate` on ~50 real releases.
3. Run the retro study on 15 to 20 past deals; review the first learning proposals once real behaviour exists.
4. Show the price move next to signals on the dashboard (the price source exists; only the display is missing).
5. Claim staking and permit registries for the first jurisdiction (v2 signals), with property-to-ticker linking.
6. Weekly sample check of documents classified as noise, with the result recorded (false-negative rate).
7. Telegram Mini App version of the dashboard; per-user event-type muting.
8. Move from SQLite to Postgres when there are more than a few dozen users.
