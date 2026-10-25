# Prompt and rule change log

Feeds the AI Orchestration Log. One entry per change to a prompt in `app/ai.py` or a weight in `app/scoring.py`.

| Date | File | What changed | Why (evidence) | Result |
|---|---|---|---|---|
| 2026-09-22 | ai.py, scoring.py | Initial prompts and starting weights | Starting guesses from Session 3 plan | To be measured with `cli evaluate` |
| | scoring.py | "already moved" rule now receives real price data | AI grader noted untested assumptions | Check in /admin/tuning once ratings exist |
