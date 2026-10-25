# Junior Signals: architecture

**Junior Signals** turns newswire releases and SEC filings about junior mining and oil & gas stocks into scored, source-quoted signals (strategic stakes, ownership threshold crossings, M&A-type and development-stage hires, private placements), held for founder review when high-impact and delivered through a web dashboard and a Telegram bot.

This is the project's architecture document. It supersedes our earlier `scope.md` and the 6 Oct 2026 `ARCHITECTURE.md` draft (neither is in this repository). Where they disagree, this file describes the code. Day-to-day notes for Claude Code are in `CLAUDE.md`; prompt and weight changes are logged in `docs/prompt_log.md`.

## Status on 25 October 2026

**Runs today (locally, on our laptops):**
- The signals dashboard (score, rule-by-rule breakdown, quotes, source link) and the `/admin` review queue.
- The full pipeline below, from document to scored signal, end to end inside the automated test suite (36 tests, `python -m pytest -q`). The tests replace the AI and Telegram calls with fakes, so they prove the pipeline logic, scoring and delivery rules, not the AI's accuracy on real filings.

**Coded but not yet proven with real users or real data:**
- The pipeline on real releases with live AI calls (`python -m app.cli run`). `config/universe.yaml` still holds only the two placeholder tickers. The first real test case is Abitibi Metals (CSE: AMQ), Newsfile company feed `https://feeds.newsfilecorp.com/company/11775`.
- Telegram bot delivery (watchlist, threshold, feedback buttons). No signal has been sent to a real user.
- Scheduler, weekly digest, retro-signal study mode, `/admin/tuning` report, optional Stooq prices, Docker/Render/GitHub Actions files. Not deployed.

**Cut at the Session 6 hackathon:**
- Signal Ledger (append-only, hash-chained record of every signal sent) and the public `/track-record` page. Both are designed (Session 5 handout) and not built. A track record is only meaningful once real signals exist.

**Demo data:** `python -m app.cli seed-demo` (or the "Load demo data" button) inserts four fake signals on two fake tickers, `EXMP.V` and `DEMO`, with `example.com` source links. Demo signals are test data and are never presented as real.

## How a document becomes a signal

| Step | Who | What happens |
| --- | --- | --- |
| 1. Collect | Code (`collectors.py`) | Reads newswire RSS feeds per ticker and SEC EDGAR filings by CIK (13D/13G and amendments, 8-K, 6-K), as configured in `config/universe.yaml`. SEDAR+ and SEDI documents are added by hand (`/admin` or `cli ingest`), not scraped. Known URLs are skipped. |
| 2. Classify | AI (`MODEL_CLASSIFY`) | Labels each new document as `strategic_investment`, `ownership_threshold`, `key_hire_ma`, `key_hire_development`, `private_placement` or `noise`. |
| 3. Extract (Maker) | AI (`MODEL_EXTRACT`) | Fills a fixed set of fields for that event type. Every field must carry the verbatim quote it came from. |
| 4. Verify (Grader) | Code + AI (`MODEL_GRADE`) | The quote must appear in the document text, and a second model must confirm the field follows from it. One retry; fields that still fail are dropped, not guessed. |
| 5. Score | Code (`scoring.py`) | Points from `WEIGHTS` and `THRESHOLDS`; the breakdown is stored and shown with the signal. Tiers: alert (8 or more), feed (4 to 7), archive (below 4). Duplicates within 30 days are dropped. |
| 6. Review | Human (`/admin`) | Alert-tier signals are stored as `pending_review` until a founder approves, edits (score changes are shown with their reason), sends feed-only, broadcasts or rejects. Documents classified as noise are listed so a founder can promote a missed event. |
| 7. Deliver | Code (`publish.py`, `bot.py`) | Dashboard feed for every published signal; Telegram message to users watching the ticker above their threshold, never sent twice; Useful / Not useful feedback. |

Runs once with `python -m app.cli run`, on a schedule with `RUN_SCHEDULER=1` and `PIPELINE_INTERVAL_HOURS` (default 0.5, i.e. every 30 minutes; read at startup), or from host cron. Model names are set in `.env`.

## Architectural principles

1. **Every fact is traceable to a primary source.** No signal leaves the system without a link to the original document, and no extracted field is stored unless its verbatim quote is found in that document and confirmed by a checking model.
2. **AI only where judgment about text is needed.** Classification, extraction and fact checking are model calls. Collection, scoring, deduplication, delivery and access control are ordinary code.
3. **Scoring is explainable by construction.** Points come from a readable rule table, and every signal shows the lines that produced its score. No learned model, no hidden weighting.
4. **A human owns the decision to publish anything high-impact.** Alert-tier signals wait in a review queue; reviewer edits and score changes are shown to users with their reason.
5. **Boring, replaceable infrastructure.** One process, one file-based database, no queues or external services beyond the two APIs we need.

## Stack and why

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | One language for pipeline, web app and evaluation scripts |
| Web | FastAPI with Jinja templates, server-rendered HTML | The dashboard is lists and forms; no build step or second language |
| Database | SQLite via plain `sqlite3` | A single file we can open, copy and inspect; queries stay visible |
| AI | Anthropic API, forced tool calls | Structured output without parsing free text; cheap model classifies, stronger model extracts and grades |
| Delivery | Telegram Bot API over HTTPS; Telegram Login Widget | Accounts without passwords or email; alerts land where users already are |
| Collection | `httpx`, `feedparser`, `beautifulsoup4` | Newswire RSS and EDGAR are plain HTTP and XML |
| Prices | Stooq daily CSV (optional) | Free and keyless; the one rule that uses it is skipped if it fails |
| Scheduling | APScheduler in-process, or host cron | No broker or worker fleet |
| Tests | `pytest`, AI and Telegram faked | Runs with no keys and no network, on every push |
| Deployment | Docker, Render, GitHub Actions (files present, not deployed) | A container and a persistent disk |

## Dependencies we deliberately avoid

| Avoided | Why |
| --- | --- |
| Frontend framework (React, Next.js) | A build pipeline and client state for pages that render lists |
| ORM (SQLAlchemy) | Hides the short, explicit SQL we need to reason about |
| Task queue (Celery, Redis) | The pipeline is a scheduled batch over a few dozen tickers |
| Telegram bot framework | We use a handful of endpoints; a framework brings an async stack for about 60 lines of HTTP |
| Agent or chain frameworks (LangChain and similar) | Our flow is a fixed sequence; abstractions would hide the prompts, which are the part we own |
| Vector database or RAG | One document at a time against a fixed schema; nothing to retrieve semantically |
| Paid market-data aggregators | The filings and releases are free and public |
| Scrapers for SEDAR+, SEDI, LinkedIn | Their terms restrict automated access; those documents are added by hand |
| Postgres, for now | SQLite covers the MVP; moving is a day of work when needed |

## Tradeoffs consciously accepted

| Tradeoff | What we give up | Why we accept it | Revisit when |
| --- | --- | --- | --- |
| SQLite, single worker | Concurrent writers, scaling | Simplicity during the MVP | More than a few dozen active users |
| Synchronous batch pipeline | One slow document delays the run | A handful of documents per hour | Coverage grows well beyond 30 tickers |
| Human review of every alert-tier signal | Speed, scale | An invented fact costs a user money | Measured error rate is low enough to auto-publish some event types |
| Strict quote verification | Some correct fields are dropped | A missing field degrades a signal; an invented one destroys trust | Grader rejection rates show the loss is material |
| Rule-based scoring, no ML | Patterns we have not articulated | No labelled data; explainability is a feature | Hundreds of rated signals exist |
| Telegram-only accounts | Users without Telegram | No passwords or email delivery | A real user is lost because of it |
| Five event types, no permits or claim staking | Arguably the most valuable signals | Linking a claim to a ticker is the hardest part of the problem | The basic loop is proven with real users |
| No price history | Volume and volatility context | Only one rule needs price | Price becomes part of scoring |
| Approximate FX, manual market caps | Precision in the "large relative to market cap" rule | The rule needs an order of magnitude | Deals near the threshold flip tiers often |

## Open assumptions

| # | Assumption | Status (25 Oct) | How we test it |
| --- | --- | --- | --- |
| 1 | AI extraction is accurate and cheap enough on real releases | Untested on real data | Run the AMQ feed first, then `cli evaluate` on about 50 real releases: field accuracy, invented facts, cost |
| 2 | These signals appear early enough to act on | Untested | Retro study on 15 to 20 past deals (`cli retro`) |
| 3 | Traders act on the feed, not just read it | Interviews confirm the need, not the behaviour | Demo to our first investor before Class 7, then real usage by interviewees |
| 4 | Newswire RSS plus EDGAR covers most events without automating SEDAR+ | Untested | One week in parallel with manual SEDAR+ checks |
| 5 | The classifier rarely discards real events | Untested | Weekly sample of noise-labelled documents in `/admin` |
| 6 | Clusters of signals on one name matter more than single events | Assumed in scoring weights | Retro study and `/admin/tuning` |
| 7 | Target users are on Telegram | Partly supported by interviews | Onboarding friction with first users |
| 8 | Users will pay about $25 a month | Untested; not asked in interviews | Ask for payment after sustained use |
| 9 | Scoring weights are roughly right | Starting guesses | Retro study, then useful rate per rule |

## Relationship to scope.md

`scope.md` was written as the plan before most of the code existed, was never updated as the code changed, and is not part of this repository. It describes the Signal Ledger and `/track-record` page as next steps; both were cut at Session 6 and are not in the code.

## Changelog

- **6 Oct 2026:** first version as `ARCHITECTURE.md`.
- **25 Oct 2026:** renamed to `architecturedoc.md` and added to the repository; checked against the code; added the status section and the document-to-signal walkthrough; corrected the event-type count in the tradeoffs table (five, not three); documented the scheduler default (30 minutes); recorded the Signal Ledger and `/track-record` page as cut; marked `scope.md` superseded.
