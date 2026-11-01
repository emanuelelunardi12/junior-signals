# Junior Signals: architecture

**Junior Signals** turns newswire releases and SEC filings about junior mining and oil & gas stocks into scored, source-quoted signals (strategic stakes, ownership threshold crossings, M&A-type and development-stage hires, private placements). Signals are **published automatically** the moment they are scored, on a web dashboard and through a Telegram bot. Since 1 November 2026 the product is also a **living product**: it records how people use published signals and proposes changes to its own scoring, which a founder approves or rejects.

This is the project's architecture document. It supersedes our earlier `scope.md` and the 6 Oct 2026 `ARCHITECTURE.md` draft (neither is in this repository). Where they disagree, this file describes the code. Day-to-day notes for Claude Code are in `CLAUDE.md`; prompt and weight changes are logged in `docs/prompt_log.md`.

## Status on 1 November 2026

**Runs today (locally, and in the automated test suite):**
- The full alert path, document to published signal, with no human step (96 tests, `python -m pytest -q`; AI and Telegram are faked, so the tests prove the logic, not the AI's accuracy on real filings).
- The red-line checks, behaviour tracking, the learning loop with founder approval, the AI insight job, the expanded ticker universe and the security measures below, each with tests.
- Checked in a real browser (headless Chrome, 1280 px and 390 px wide): dashboard, admin, tuning and privacy pages render without horizontal scrolling, and the dashboard records impressions, opens, reading time, scroll-past and source clicks.

**Coded but not yet proven with real users or real data:**
- Deployment. The Docker image, `render.yaml` Blueprint, health check and production guards are ready and a production-like start was smoke-tested locally, but **the app is not deployed and there is no public URL yet**. Deploying needs a founder to confirm the paid Render plan (about $7/month plus $0.25/month for the 1 GB disk) and to type in the secrets; see "Deploy" below.
- Live AI calls on real releases, Telegram delivery to real users, and the learning loop on real behaviour (it needs at least 30 impressions and 5 ratings per event type before it proposes anything).
- The SEC listing download (`sec.gov` is not reachable from our build sandbox; the parser is tested against the documented file format).

**Cut or not built:** see "Designed but not built" at the end.

**Demo data:** `python -m app.cli seed-demo` (or "Load demo data" on a local dashboard) inserts four fake signals on two fake tickers, `EXMP.V` and `DEMO`, with `example.com` source links. Demo data and the two placeholder tickers (`demo: true` in `universe.yaml`) are refused or skipped on any public https deployment.

## How a document becomes a signal

| Step | Who | What happens |
| --- | --- | --- |
| 1. Collect | Code (`collectors.py`) | Newswire RSS feeds per ticker and SEC EDGAR filings by CIK (13D/13G and amendments, 8-K, 6-K), for tickers in `config/universe.yaml` and any listing at least one user watches. SEDAR+ and SEDI documents are added by hand, not scraped. Known URLs are skipped. |
| 2. Classify | AI (`MODEL_CLASSIFY`) | `strategic_investment`, `ownership_threshold`, `key_hire_ma`, `key_hire_development`, `private_placement` or `noise`. |
| 3. Extract (Maker) | AI (`MODEL_EXTRACT`) | A fixed set of fields per event type; every field carries the verbatim quote it came from. |
| 4. Verify (Grader) | Code + AI (`MODEL_GRADE`) | The quote must appear in the document (code), and a second model must confirm the field follows from it. One retry; fields that still fail are dropped. |
| 5. Score | Code (`scoring.py`) | Points from the rule table, using the **weights in force** (code defaults plus founder-approved changes). The breakdown, the rule behind each line and the weights version are stored with the signal. Tiers: alert (8+), feed (4 to 7), archive (below 4). Duplicates within 30 days are dropped. |
| 6. Red lines | Code (`guardrails.py`) | Advice-like AI headlines are replaced by a rule-built neutral headline. Then, before anything is shown or sent: an http(s) source link and at least one quote found verbatim in the document. A signal that fails is **withheld automatically**, never queued for a person. |
| 7. Publish | Code (`publish.go_live`) | Live on the dashboard at once; Telegram message to users watching the ticker above their threshold; never twice to the same user (`deliveries`); a network error for one user is logged and does not stop the others. |
| 8. Learn | Code + founder (`learning.py`, `/admin/tuning`) | Behaviour on published signals feeds a daily learning cycle that proposes weight changes. Only founder-approved changes affect scoring, and only for signals scored afterwards. |

The pipeline runs every 30 minutes on the server (`RUN_SCHEDULER=1`, `PIPELINE_INTERVAL_HOURS=0.5`), or once with `python -m app.cli run`.

## The automatic alert path, and why publishing is automatic by design

Our edge is speed: an operational signal is worth most in the hours after the filing. A human approval step before publishing would make every alert wait for whichever founder is awake, which is exactly the delay retail traders told us they suffer with manual checks. So the path from document to published alert has **no human gate** (`pipeline.process_document` calls `publish.go_live` directly; a test fails if a review status reappears in the pipeline).

Publishing one alert fast is safe enough because of what every alert carries, checked in code: a link to the primary source and a verbatim quote from it, a rule-by-rule score breakdown, and no advice. A wrong alert is visible, checkable against its own source, and correctable: a founder can **retract** a live signal from `/admin`, and everyone who received it gets a correction (`publish.retract`, using `deliveries`). Retraction is after the fact and outside the alert path.

Founders still create drafts in one case: a document they paste by hand into `/admin` (SEDAR+, SEDI, company sites) opens as a draft, so the author can check what the AI extracted before choosing how to send it. The automatic pipeline never creates drafts.

## The red lines (enforced in code)

| Red line | Where | What happens on failure |
| --- | --- | --- |
| Every published signal has a primary-source link (http/https with a real host) | `guardrails.violations`, called by `publish.go_live` | Status `withheld` with the reason; not shown, not sent; listed in `/admin` |
| Every published signal has at least one quote found verbatim in its source document (re-checked at publish time) | same | same |
| Nothing we publish is buy/sell advice | `guardrails.advice_phrase` on our own words (headline, extracted values), not on verbatim quotes, which are attributed to the source | The AI headline is replaced by a neutral rule-built one and the alert goes out; any remaining advice-like text withholds the signal. Every message also carries "Information only, not investment advice." |

These are guarantees, not speed barriers: they run in milliseconds and need no person.

## Where the human sits: the learning path

The dangerous step is not publishing one fast, quote-checked alert. It is the system **silently rewiring how it scores everything** from noisy engagement data: drifting overnight, rewarding clickbait (signals that get clicked but are not useful), and losing the ability to explain why a signal scored what it did. So the human reviews the **proposed self-modifications** before they take effect:

- the learning loop only ever creates `tuning_proposals`; only founder decisions write `weight_changes` (`learning.approve()`, and `learning.revert()` to undo one), reachable only from `/admin/tuning` by a founder in `ADMIN_IDS`; a double submit cannot apply a proposal twice;
- every proposal shows the behaviour data behind it; the founder approves or rejects with an optional note; approved changes can be reverted;
- an approved change applies to signals scored after it; published signals keep the score and breakdown they were given, so every score stays explainable.

## Behaviour tracking (the learning fuel)

Recorded on published signals, keyed by a **daily pseudonymous id**, never the Telegram user id (`app/behaviour.py`, `app/static/track.js`):

| Signal | Stored | How it is captured |
| --- | --- | --- |
| Impression | timestamp, signal id, event type, ticker, session id, position in the list | at least half the card on screen for one second |
| Open | same, without position | user expands "Why this score" |
| Dwell | seconds the opened signal stayed open on a visible page, capped at 600 | measured in the browser, paused while the tab is hidden |
| Scroll-past | same | card was seen, not opened, and scrolled above the viewport |
| Source click | same | click on "Read source" |
| Like / dislike / cleared | same, surface web or Telegram | Useful / Not useful taps, recorded server-side with the rating itself (the events API refuses them); one current rating per user and signal |
| Watchlist add / remove, event-type filter | timestamp, session id, surface, ticker or event type | `watchlist_events` |
| Telegram impressions | per user, for corrections | `deliveries` |
| Free-text comment (optional) | timestamp, session id, signal id, cleaned text | side input only: read by founders, never used by the learning loop, never sent to the AI |

Event type and ticker are copied from the signal on the server, and browser events are accepted only for signals the server showed that session. The id is a keyed hash (HMAC with the server secret) of the user id, the surface and the UTC date: computed on the server, never sent to the browser, changing daily, and deleted on `/stop` (we can recompute it). It is **pseudonymous, not anonymous**: because ratings are also stored with the account so the buttons show them, someone with database access and the secret could link a day's reading to a user; `/privacy` says so. Opt-out: a dashboard checkbox, `/tracking off` in Telegram, or the browser's Global Privacy Control header. Explicit ratings still count in per-event-type totals (they are needed for the buttons' state). Raw events are deleted after 180 days (`BEHAVIOUR_RETENTION_DAYS`). The plain-language note is at `/privacy`.

Per-event-type engagement (`behaviour.engagement_by_event_type`) counts distinct (daily id, signal) pairs, so reloading or logging in again the same day does not inflate the counts: open rate, click-through, skip rate, average reading time, like rate, Telegram deliveries.

## The learning loop

| Stage | What it does |
| --- | --- |
| Input | Behaviour per event type and per scoring rule (each signal stores the rule key behind every breakdown line), over the last 30 days, plus the rules themselves. |
| Learning | `learning.run_cycle()` (daily, or the button on `/admin/tuning`) aggregates deterministically and proposes **+1** when users consistently **like and engage** (like rate ≥ 70% of at least 5 ratings, and open rate or click-through at least 20% above the average of all types), or **−1** when they consistently **dislike** (like rate ≤ 35%) or **ignore** (60%+ scrolled past and open rate ≤ 60% of average). Needs 30+ impressions; rules need 8+ ratings. |
| Human safety check | The proposal waits on `/admin/tuning` with its evidence. Nothing is applied until a founder approves. |
| Improvement | On approval, a row is appended to `weight_changes`; its id is the new weights version. Every signal scored afterwards uses the new weight and is stamped with the version, so future alerts of that type are scored and tiered (and therefore delivered) differently. |
| Measurement | Each cycle stores engagement per event type (`engagement_snapshots`) and headline metrics (`learning_cycles`): alert-tier open rate, alert-tier like rate, click-through on the top three positions of the feed. Each approval stores a before snapshot; `/admin/tuning` shows before vs after on signals scored with the new weights. |

Safeguards in code: engagement alone never raises a weight (clickbait guard); one step at a time; weights stay within bounds and at most 2 from the code default (beyond that, change the code and log it); one open proposal per weight; a proposal made against a value that has since changed is marked superseded; every change is append-only and revertible.

**AI insight job** (`app/insights.py`, `/admin/insights`, weekly or on demand): sends the aggregated numbers only (no comments, no session ids, no tickers) to the AI, which clusters patterns and suggests weights to revisit through a forced tool call. Every number the AI cites is checked against what we sent, like the quote check for extraction; unverified suggestions are labelled. Suggestion-only: a founder can turn a raise/lower suggestion into a normal proposal, which still needs approval on `/admin/tuning`.

## Ticker universe and coverage

Being selectable is not the same as being covered.

| Exchange | Where the list comes from | Selectable | Automatic event coverage |
| --- | --- | --- | --- |
| NASDAQ | SEC `company_tickers_exchange.json` (US government data, redistributable; includes CIKs). Downloaded at first start and weekly. | Yes, once the server has downloaded the file | **Yes, EDGAR**: 13D/13G, 8-K, 6-K for any name a user watches, through its CIK. No newswire feeds unless configured. |
| NYSE | same | same | same |
| TSX | TMX issuer list, **loaded by hand** by a founder (admin upload or `cli load-listings --source tmx`). TMX's terms forbid copying or redistribution without consent, so we neither scrape nor bundle it. | Only after a founder loads the list (plus names in `universe.yaml`) | **Only where a newswire RSS feed is configured in `universe.yaml`**, or cross-listed names a founder adds with a CIK. SEDAR+ is never scraped. |
| TSX Venture | same | same | same |

Each listing shows its coverage to users ("EDGAR", "newswire", "newswire + EDGAR" or "not covered yet"). A user can still add an uncovered name; the app says plainly that only hand-added signals will appear, and logs it as a request so founders see where to add feeds. Collectors only run for configured or watched tickers, so thousands of selectable listings cost nothing until someone follows them. OTC and Cboe listings in the SEC file are ignored for now. Before showing a TMX list to many users we should ask TMX for permission.

## Data model changes (1 Nov 2026)

New tables: `deliveries`, `behaviour_events`, `watchlist_events`, `comments`, `learning_cycles`, `engagement_snapshots`, `tuning_proposals`, `weight_changes`, `ai_insights`, `listings`. New columns: `signals.status_note`, `signals.rule_keys`, `signals.weights_version`, `users.tracking_opt_out`, `tickers.configured`. Signal statuses are now `queued`, `published`, `withheld`, `retracted`, `draft`, `rejected`, `retro`; older databases migrate `approved` to `published` and `pending_review` to `draft` (`db.DATA_MIGRATIONS`).

New endpoints: `POST /api/events`, `GET /api/tickers`, `POST /comments/{id}`, `GET /privacy`, `GET /healthz`, `POST /admin/signal/{id}/retract`, `POST /admin/tuning/run`, `POST /admin/tuning/{id}/approve|reject|revert`, `GET /admin/insights`, `POST /admin/insights/run`, `POST /admin/insights/{id}/propose/{n}`, `POST /admin/listings`, `POST /admin/webhook`. Changed: `/admin` (live feed, withheld list, drafts), `/admin/tuning` (proposals and metrics), `/dashboard` (`type` filter, `q` search), `/feedback` (toggle, JSON), `/settings` (opt-out). Bot: `/privacy`, `/tracking on|off`. CLI: `learn`, `insights`, `load-listings`.

## Deploy

`render.yaml` is a Render Blueprint: Docker web service, `starter` instance (0.5 CPU, 512 MB, about $7/month), 1 GB persistent disk at `/data` for the SQLite file ($0.25/month), health check `/healthz`, scheduler on, generated `SESSION_SECRET` and `TELEGRAM_WEBHOOK_SECRET`, `BASE_URL` taken from Render's `RENDER_EXTERNAL_URL`. A founder deploys it (New → Blueprint → this repository), types the secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `ADMIN_IDS`, `ANTHROPIC_API_KEY`, `SEC_USER_AGENT`), sets the bot's domain with BotFather `/setdomain`, then presses "Connect the Telegram webhook" in `/admin`. The free instance type is not used because it cannot keep the database across deploys.

## Security

- Sessions: signed cookie, `HttpOnly`, `SameSite=Lax`, `Secure` on https; Telegram login hash verified, login links valid for one hour, and no login at all while `TELEGRAM_BOT_TOKEN` is empty (the key would otherwise be computable by anyone).
- The development test login is **forced off** on any https `BASE_URL` (it lets anyone become any user, and an admin when `ADMIN_IDS` is empty); no implicit admins on public URLs. The app refuses to start on a public URL with a weak `SESSION_SECRET` or a bot without a webhook secret.
- Every state-changing request is refused if the browser marks it cross-site (`Origin`, `Sec-Fetch-Site`); 6 MB body cap by declared length, and the events API also counts streamed bytes (16 KB); Telegram webhook secret compared in constant time; malformed updates never crash it.
- Headers: Content-Security-Policy with no inline script, `nosniff`, frame `DENY`, referrer policy, permissions policy, HSTS on https. API docs routes are off.
- Input validation: pydantic models for behaviour events (allowed actions, bounded numbers, max 50 per batch, 16 KB); ticker symbols by pattern; search text bounded and SQL `LIKE` wildcards escaped; comments cleaned of control characters, length-capped and escaped on output; bounded admin form fields; parameterised SQL throughout.
- Rate limits (in memory, per logged-in account however many sessions it opens, else per address): login, behaviour events, ratings, comments, search, watchlist, settings, AI insight runs. The address fallback only covers logged-out requests and can be spoofed behind a proxy (`--forwarded-allow-ips='*'` on Render); founder approval is the backstop against inflated metrics.
- Errors: friendly 403/404/429/500 pages (JSON for `/api`), details only in the server log.
- Secrets: none in the repository (history scanned); `.env` and the database are git- and docker-ignored; Render secrets are `sync: false` or generated.
- Data: one SQLite connection per thread (a stress test showed the old shared connection lost rows under concurrent writes), WAL mode, write lock.

## Architectural principles

1. **Every fact is traceable to a primary source.** No signal goes live without a link to the original document and a verbatim quote found in it; no extracted field is stored unless its quote is found and a checking model confirms it.
2. **AI only where judgment about text is needed.** Classification, extraction, fact checking, and the weekly suggestion-only insight job. Collection, scoring, red lines, publishing, behaviour aggregation, proposals and access control are ordinary code.
3. **Scoring is explainable by construction.** A readable rule table; every signal shows the lines that produced its score and records the weights version used. Learning changes the numbers in the table, one approved step at a time; it never adds a hidden model.
4. **Publishing is automatic; changing how we score is a human decision.** Alerts go out without a gate, protected by red lines in code. Self-modifications of the scoring wait for a founder.
5. **Boring, replaceable infrastructure.** One process, one file-based database, no queues or external services beyond the two APIs we need.

## Stack and why

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | One language for pipeline, web app and evaluation scripts |
| Web | FastAPI with Jinja templates, server-rendered HTML, two small vanilla JS files | Lists and forms; tracking and live search are progressive enhancements |
| Database | SQLite via plain `sqlite3`, WAL, one connection per thread | A single file we can open, copy and inspect; queries stay visible |
| AI | Anthropic API, forced tool calls | Structured output without parsing free text |
| Delivery | Telegram Bot API over HTTPS; Telegram Login Widget | Accounts without passwords or email; alerts land where users already are |
| Collection | `httpx`, `feedparser`, `beautifulsoup4` | Newswire RSS and EDGAR are plain HTTP and XML |
| Validation | pydantic (already part of FastAPI) | Strict schemas for browser-sent events |
| Scheduling | APScheduler in-process | Pipeline, learning, insights, listings, digest without a broker |
| Tests | `pytest`, AI and Telegram faked | Runs with no keys and no network, on every push |
| Deployment | Docker, Render Blueprint, GitHub Actions | A container and a persistent disk |

## Dependencies we deliberately avoid

| Avoided | Why |
| --- | --- |
| Frontend framework (React, Next.js) | A build pipeline and client state for pages that render lists |
| ORM (SQLAlchemy) | Hides the short, explicit SQL we need to reason about |
| Task queue (Celery, Redis) | The pipeline is a scheduled batch; the rate limiter fits in one process's memory |
| Analytics SaaS (Google Analytics, Mixpanel, Segment) | Sends user behaviour to third parties; we need a handful of counts per event type, kept in our own database |
| ML or bandit libraries for ranking | Learned weights would break explainability; ±1 rule changes approved by a person keep it |
| Telegram bot framework | We use a handful of endpoints |
| Agent or chain frameworks (LangChain and similar) | Our flow is a fixed sequence; abstractions would hide the prompts |
| Vector database or RAG | One document at a time against a fixed schema |
| Paid market-data aggregators | The filings and releases are free and public |
| Scrapers for SEDAR+, SEDI, LinkedIn, TMX listings | Their terms restrict automated access; those are loaded by hand |
| Postgres, for now | SQLite covers the MVP |

## Tradeoffs consciously accepted

| Tradeoff | What we give up | Why we accept it | Revisit when |
| --- | --- | --- | --- |
| No human gate before publishing | A person catching a bad alert before users see it | Speed is the edge; red lines in code plus retraction with corrections | Withheld or retracted rate is material |
| Human approval for every weight change | Faster adaptation | Prevents silent drift and clickbait bias; keeps scores explainable | Proposals are routinely approved unchanged for months |
| ±1 steps, max 2 from default | Large corrections in one go | Bounded, reversible learning | Evidence is strong and a code change is logged |
| Daily pseudonymous behaviour ids | Per-user personalisation and history beyond a day | Rotates daily, never in the browser, deletable; per-event-type totals are all the loop needs | Users ask for personalised ranking |
| Ratings kept even when a user opts out of behaviour tracking | A fully silent opt-out | Ratings are explicit and needed for the buttons' state; counted only in totals | Users object |
| In-memory rate limiter, single worker | Limits per worker if we scale out | No Redis to run | More than one worker |
| SQLite, single worker | Concurrent writers across machines | Simplicity during the MVP | More than a few dozen active users |
| TMX lists loaded by hand | Automatic TSX/TSXV refresh | TMX terms forbid automated copying | TMX grants permission or a licensed feed is affordable |
| Strict quote verification | Some correct fields are dropped | An invented fact destroys trust | Grader rejection rates show the loss is material |
| Rule-based scoring, no ML | Patterns we have not articulated | Explainability is a feature | Hundreds of rated signals exist |
| Telegram-only accounts | Users without Telegram | No passwords or email delivery | A real user is lost because of it |
| Five event types, no permits or claim staking | Arguably the most valuable signals | Linking a claim to a ticker is the hardest part | The basic loop is proven with real users |
| Container runs as root | Defence in depth inside the container | Render's persistent disk is mounted root-owned; a non-root user risks a deploy that can't write the database | We verify disk permissions on Render |

## Open assumptions

| # | Assumption | Status (1 Nov) | How we test it |
| --- | --- | --- | --- |
| 1 | AI extraction is accurate and cheap enough on real releases | Untested on real data | `cli evaluate` on about 50 real releases |
| 2 | These signals appear early enough to act on | Untested | Retro study on 15 to 20 past deals (`cli retro`) |
| 3 | Traders act on the feed, not just read it | Now measurable: opens, reading time, source clicks | First weeks of real usage on the deployed app |
| 4 | Newswire RSS plus EDGAR covers most events without SEDAR+ | Untested; US names now get EDGAR automatically, TSX/TSXV still need feeds | Coverage labels and requests in `/admin`; one week in parallel with manual SEDAR+ checks |
| 5 | The classifier rarely discards real events | Untested | Weekly sample of noise-labelled documents in `/admin` |
| 6 | Clusters of signals on one name matter more than single events | Assumed in scoring weights | Per-rule engagement on `/admin/tuning` |
| 7 | Target users are on Telegram | Partly supported by interviews | Onboarding friction with first users |
| 8 | Users will pay about $25 a month | Untested | Ask for payment after sustained use |
| 9 | Scoring weights are roughly right | Starting guesses; now testable | Learning proposals with evidence, approved or rejected by a founder |
| 10 | Behaviour (opens, reading, clicks, ratings) reflects usefulness, not curiosity | Assumed; mitigated by the rule that engagement alone never raises a weight | Compare proposals with retro-study results and interviews |
| 11 | Automatic publishing produces few wrong alerts | Untested | Withheld and retracted counts in `/admin` |

## Relationship to scope.md

`scope.md` was written as the plan before most of the code existed, was never updated, and is not in this repository, so this reconciliation is against what we recorded about it on 25 October. It described the Signal Ledger and `/track-record` page as next steps; both remain cut. It assumed founder review of high-impact signals before publishing; that is **reversed** on 1 November (publishing is automatic, the human moved to the learning path). It did not include behaviour tracking, a learning loop, an expanded ticker universe or a security pass; those are new scope from the Living Products phase (Sessions 7 to 9), recorded here.

## Designed but not built

- **Deployment to a public URL**: everything is prepared; it needs a founder to confirm the Render plan and enter secrets (see "Deploy").
- **Real newswire feeds for TSX/TSXV names** in `universe.yaml`, and the TMX list itself (manual load).
- **Ranking learning beyond scoring weights** (for example a recency or novelty decay in a "Top" feed order): the feed is newest first; the loop tunes the weights that set scores, tiers and Telegram delivery.
- **Per-user event-type muting for Telegram** (would reduce noise further; the threshold covers it for now).
- **Signal Ledger and `/track-record`** (cut at Session 6).
- **Telegram source-click tracking** (would need a redirect through our server, which hides the primary-source link; we chose the visible direct link).
- **Postgres and more than one worker.**

## Changelog

- **6 Oct 2026:** first version as `ARCHITECTURE.md`.
- **25 Oct 2026:** renamed to `architecturedoc.md` and added to the repository; checked against the code; added the status section and the document-to-signal walkthrough; corrected the event-type count; documented the scheduler default; recorded the Signal Ledger and `/track-record` page as cut; marked `scope.md` superseded.
- **1 Nov 2026 (session log, Living Products phase):** publishing made automatic by design (no human gate in the alert path) with the two red lines enforced in code (`guardrails.py`), automatic withholding, and retraction with corrections; human moved to the learning path. Added behaviour instrumentation (daily pseudonymous ids, opt-out, Global Privacy Control, 180-day retention, `/privacy`); the learning loop with proposals, founder approval, revert, weights versioning and before/after metrics; the weekly suggestion-only AI insight job with verified numbers; the ticker universe for TSX, TSX Venture, NASDAQ and NYSE with honest coverage labels (SEC list automatic, TMX list manual by its terms); a security pass (headers, CSP, cross-site check, production guards, rate limits, error pages, health check); per-thread SQLite connections after a stress test exposed lost writes; UX fixes found in a real browser. An independent review then found a pre-existing critical flaw (with an empty bot token, the Telegram login could be forged for any user, admins included) and several important ones (a Telegram network error could stop delivery, one account could inflate metrics, gaps in the advice filter, before/after windows); all fixed with regression tests the same day. Tests: 36 → 96. Not deployed yet (needs founder payment confirmation and secrets).
