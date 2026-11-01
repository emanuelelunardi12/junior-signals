# Junior Signals (MVP)

Early operational signals on junior mining and oil & gas stocks, delivered by Telegram, with a small web dashboard.

**Flow:** collect documents (newswire RSS, SEC EDGAR, manual) → classify → extract with quotes (Maker) →
quote check + Grader → rule-based score → red-line checks → **published automatically** to Telegram and the dashboard.
User behaviour on published signals feeds a learning loop that **proposes** scoring changes; a founder approves them
in `/admin/tuning`. See `architecturedoc.md` for the why.

## Run it locally in 5 minutes (no API keys)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional locally; needed once you add keys
python -m app.cli init
uvicorn app.web:app --reload
```
Open http://localhost:8000, click "Log in as test user", then "Load demo data" on the dashboard. That adds
example signals and puts EXMP.V and DEMO on your watchlist. `/admin` (the Review link) shows the review queue.

Run other commands (`python -m app.cli ...`) in a **second terminal**: the one running the server is busy
and ignores what you type.

Tests: `python -m pytest -q` (AI and Telegram calls are faked).

**No "Log in as test user" button?** It is shown automatically on localhost while no bot token is configured,
so a fresh checkout works even without `.env`. If it is missing, something has set `DEV_LOGIN=0`, or a bot token
is configured, or `BASE_URL` is not localhost. Settings are read once at startup, so restart after editing
`.env`. The terminal prints what is actually in use at startup:
`[config] DEV_LOGIN=on  bot=not set  BASE_URL=http://localhost:8000  db=...`

## Connect Telegram

1. In Telegram, talk to **@BotFather**: `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`, the bot's username into `TELEGRAM_BOT_USERNAME`.
2. Telegram needs a public HTTPS address. Locally, use a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000` or ngrok) and put its URL in `BASE_URL`.
3. BotFather: `/setdomain`, choose your bot, enter the tunnel/production domain. This enables the login button.
4. `python -m app.cli set-webhook` so commands and button taps reach `/telegram/webhook`.
5. Put your own Telegram user ID in `ADMIN_IDS` and set `DEV_LOGIN=0` before anyone else uses it.

Login uses the Telegram Login Widget with `request-access="write"`, so after logging in the bot can message the
user directly. If they decline, the dashboard shows a banner with a link to start the bot.

## Run the pipeline

1. Add real tickers to `config/universe.yaml` (from your interviewees' watchlists), with RSS feeds and/or SEC CIK.
2. Set `ANTHROPIC_API_KEY`.
3. `python -m app.cli run` (collect + process). Schedule it every few hours with cron on the server.
4. Documents from SEDAR+/SEDI or company sites: save the text and run
   `python -m app.cli ingest --ticker EXMP.V --url <link> --file doc.txt`, then `python -m app.cli run --no-collect`.

## Admin

`/admin` (only for Telegram IDs in `ADMIN_IDS`). Nothing here holds an alert up: pipeline signals publish automatically.
- **Live now:** recently published signals and how many people got each. **Retract** takes one down and sends a
  correction to exactly those people.
- **Withheld:** signals that failed a red line (no source link, no verbatim quote, advice) and were never shown.
- **Your drafts:** only signals you create by hand. Edit the headline, add a score adjustment (needs a reason; users see
  it), then **Send to watchers**, **Add to feed only**, **Broadcast to everyone** or **Discard**.
- **Create a signal from a document:** paste a filing or release you read yourself (SEDAR+, SEDI), with its source link.
- **Ticker universe:** coverage per exchange; upload a TMX issuer list (CSV) for TSX or TSX Venture; refresh the SEC list.
- **Announcement to all users**, and **Connect the Telegram webhook** after deploying.

`/admin/tuning`: the learning loop. Proposed weight changes with the behaviour behind them; **Approve**, **Reject**,
later **Revert**. Also before/after results of approved changes, the learning metric per cycle, engagement per event type
and the weights in force. `/admin/insights`: the weekly AI insight job (suggestion-only) and user comments.

## Ticker universe

NASDAQ and NYSE listings come from the SEC's public file and load automatically on the server (or
`python -m app.cli load-listings --source sec [--file saved.json]`). TSX and TSX Venture lists are TMX's; their terms
forbid automated copying, so download the issuer list yourself, save the sheet as CSV and load it in `/admin` or with
`python -m app.cli load-listings --source tmx --exchange TSXV --file tsxv.csv`. Users see per ticker whether we cover it
(EDGAR, newswire, or not yet).

## Behaviour tracking and privacy

The dashboard records impressions, opens, reading time, scroll-past, source clicks, ratings and watchlist changes under
an anonymous session id (`app/behaviour.py`). Users can opt out on the dashboard, with `/tracking off` in Telegram, or
via Global Privacy Control. The user-facing note is at `/privacy`. Run a learning cycle by hand with
`python -m app.cli learn`; it only proposes.

## Weekly digest, retro study, prices

- **Weekly digest:** `python -m app.cli digest` sends each user a summary of the week's signals on their
  watchlist. Users with no signals that week get nothing.
- **Retro-signal study:** put historical documents in `retro_data/` named `TICKER__YYYY-MM-DD__slug.txt`, then
  `python -m app.cli retro --docs retro_data --out data/retro_timeline.csv`. Signals are scored and stored with
  their original date, marked `retro`, and never sent to anyone. Add each deal's announcement date to the CSV to
  see how early the signals appeared.
- **Price moves:** set `PRICE_SOURCE=stooq` and a `price_symbol` per ticker in `universe.yaml` to activate the
  "already moved" rule. Without it the rule is skipped and everything else works.
- **Documents the AI skipped:** `/admin` lists recent documents classified as noise. Check a sample every week
  and promote any real event; this is the only check that catches missed signals.
- **Weight tuning:** `/admin/tuning` (see Admin above).

## Scheduling

Two options, don't use both:
- **Host cron** (simplest, works with several web workers): `python -m app.cli run` every few hours and
  `python -m app.cli digest` weekly.
- **In-process scheduler:** set `RUN_SCHEDULER=1` with a single worker. It runs the pipeline every
  `PIPELINE_INTERVAL_HOURS` and the digest on `DIGEST_DAY`/`DIGEST_HOUR`, and messages `ADMIN_IDS` if a run fails.

## Handout 5 test (Step-by-step vs Maker and Grader)

Put ~50 real releases in `eval_data/docs/` and your manual answers in `eval_data/answer_key.csv`
(one example included), fill `MODEL_PRICES`, then `python -m app.cli evaluate`. It prints accuracy, invented
facts, time, tokens and cost for both setups.

## Deploy (Render)

1. Render dashboard → **New → Blueprint** → pick this repository. `render.yaml` creates a Docker web service
   (`starter`, about $7/month) with a 1 GB disk at `/data` ($0.25/month) for the SQLite file, health check `/healthz`.
2. Fill in the secrets it asks for: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `ADMIN_IDS` (your Telegram user id),
   `ANTHROPIC_API_KEY`, `SEC_USER_AGENT` ("JuniorSignals your@email"). `SESSION_SECRET` and `TELEGRAM_WEBHOOK_SECRET`
   are generated; `BASE_URL` defaults to the Render URL.
3. BotFather: `/setdomain` → the Render domain (enables the login button).
4. Log in with Telegram, open `/admin`, press **Connect the Telegram webhook**.

The app refuses to start on an https URL with a weak `SESSION_SECRET`, and the test login is always off there.
GitHub Actions runs the test suite on every push. Move to Postgres when there are more users.
