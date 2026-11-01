# Prompt and rule change log

Feeds the AI Orchestration Log. One entry per change to a prompt in `app/ai.py` or a weight in `app/scoring.py`.

| Date | File | What changed | Why (evidence) | Result |
|---|---|---|---|---|
| 2026-09-22 | ai.py, scoring.py | Initial prompts and starting weights | Starting guesses from Session 3 plan | To be measured with `cli evaluate` |
| | scoring.py | "already moved" rule now receives real price data | AI grader noted untested assumptions | Check in /admin/tuning once ratings exist |
| 2026-11-01 | insights.py | New prompt + tool `record_insights` for the weekly AI insight job (clusters, suggested weights, caveats) | Learning loop needs a second view on behaviour patterns; numbers-only input, every cited number verified in code | Suggestion-only; a founder turns a suggestion into a proposal and approves it in /admin/tuning |
| 2026-11-01 | scoring.py, learning.py | Weights are no longer edited in code only: the learning loop PROPOSES ±1 changes from behaviour; approved changes are rows in `weight_changes` (version stamped on each signal) | Session 7 "living product": learn from real user behaviour without silent drift | Code defaults unchanged; first measurable changes need real users (min 30 impressions, 5 ratings per type) |
| 2026-11-01 | pipeline.py, guardrails.py | Advice-like AI headlines are replaced automatically by a rule-built neutral headline | Red line: never emit buy/sell advice, without a human gate in the alert path | Logged in the signal's `rejected` list |
