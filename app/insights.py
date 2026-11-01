"""AI insight job: a second pair of eyes on the behaviour numbers. SUGGESTION-ONLY.

Sends the aggregated engagement per event type and per scoring rule (numbers only: no comments, no session ids,
no tickers, no user data) to the AI, which clusters the patterns and suggests which weights to revisit.

Rules, consistent with the rest of the system:
- The output is a forced tool call (structured), never free text we parse.
- Every number the AI cites must appear in what we sent; this is checked in code, like the quote check for
  extraction. Suggestions citing numbers we did not send are shown as "unverified".
- A suggestion changes nothing. A founder can turn one into a proposal, which still has to be approved on
  /admin/tuning like any other. Comments are never sent: they are user-written text (privacy, prompt injection).
"""
import json
import re

from app import ai, behaviour, config, db, learning, scoring

MIN_EVENTS = 20   # below this there is nothing worth asking about

SYSTEM = """You review aggregated engagement numbers for a signals product that scores news about junior mining
and oil & gas companies with a transparent rule table. Your job is to find patterns and suggest which scoring
weights the founders should revisit. You change nothing yourself; a founder decides.
Rules:
- Use only the numbers in the input. Cite them exactly as written there (same decimals), in the numbers lists.
- Small samples are weak evidence: say so in caveats instead of making a suggestion.
- Do not reward engagement alone: a type that is clicked often but rated not useful is a warning sign, not a win.
- Suggest at most five weights. Use direction "review" when the evidence is mixed."""

TOOL = {
    "name": "record_insights",
    "description": "Record clusters of behaviour patterns and suggested weights to revisit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clusters": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"},
                "members": {"type": "array", "items": {"type": "string"}},
                "pattern": {"type": "string"},
                "numbers": {"type": "array", "items": {"type": "number"}},
            }, "required": ["label", "members", "pattern", "numbers"]}},
            "suggestions": {"type": "array", "items": {"type": "object", "properties": {
                "weight_key": {"type": "string", "enum": list(scoring.RULE_LABELS)},
                "direction": {"type": "string", "enum": ["raise", "lower", "review"]},
                "rationale": {"type": "string"},
                "numbers": {"type": "array", "items": {"type": "number"}},
            }, "required": ["weight_key", "direction", "rationale", "numbers"]}},
            "caveats": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["clusters", "suggestions", "caveats"],
    },
}


def build_input(window_days: int = learning.WINDOW_DAYS) -> dict:
    since = learning._ago(window_days)
    weights = learning.effective_weights()
    by_type = behaviour.engagement_by_event_type(since=since)
    by_rule = learning.engagement_by_rule(since=since)
    keep = ("impressions", "opens", "source_clicks", "scroll_pasts", "telegram_deliveries", "likes", "dislikes",
            "open_rate", "like_rate", "ctr", "skip_rate", "avg_dwell")
    return {
        "window_days": window_days,
        "event_types": {et: {k: m.get(k) for k in keep if k in m} for et, m in by_type.items()},
        "rules": {r: {k: m.get(k) for k in keep if k in m} for r, m in by_rule.items()},
        "weights_in_force": {k: scoring.get_weight(weights, k) for k in scoring.RULE_LABELS},
        "headline": learning.headline_metrics(since=since),
    }


def _numbers_in(obj) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers_in(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers_in(v)]
    return []


def _cited_ok(n, known: list[float]) -> bool:
    """A cited number counts if it is one we sent (to 4 decimals), or a ratio we sent written as a percentage."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return False
    return any(abs(n - k) <= 5e-5 or (0 <= k <= 1 and abs(n - k * 100) <= 0.05) for k in known)


def _scope_numbers(sent: dict, members) -> list[float]:
    """Numbers that belong to the event types / rules a claim is about, plus the global headline metrics.
    Checking against the whole input would let any small count or weight 'verify' anything."""
    out = _numbers_in(sent.get("headline", {}))
    for m in members or []:
        m = str(m)
        key = m[5:] if m.startswith("base.") else m
        out += _numbers_in(sent["event_types"].get(key, {})) + _numbers_in(sent["rules"].get(m, {}))
    return out


def verify(output: dict, sent: dict) -> dict:
    """A cluster or suggestion is verified only if it cites at least one number and every number it cites is
    one we sent for the event types or rules it is about."""
    def ok(nums, known):
        return bool(nums) and all(_cited_ok(n, known) for n in nums)
    for c in output.get("clusters", []):
        c["verified"] = ok(c.get("numbers"), _scope_numbers(sent, c.get("members")))
    output["suggestions"] = [s for s in output.get("suggestions", []) if s.get("weight_key") in scoring.RULE_LABELS]
    for s in output["suggestions"]:
        s["verified"] = ok(s.get("numbers"), _scope_numbers(sent, [s["weight_key"]]))
        s["label"] = scoring.RULE_LABELS[s["weight_key"]]
    return output


def run(window_days: int = learning.WINDOW_DAYS) -> int:
    """Run the insight job once. Returns the ai_insights row id. Never changes a weight."""
    sent = build_input(window_days)
    events = db.get().execute("SELECT COUNT(*) n FROM behaviour_events WHERE created_at >= ?",
                              (learning._ago(window_days),)).fetchone()["n"]
    ratings = sum(m.get("likes", 0) + m.get("dislikes", 0) for m in sent["event_types"].values())
    status, note, output = "ok", "", None
    if events + ratings < MIN_EVENTS:
        status, note = "skipped", f"Only {events} events and {ratings} ratings in {window_days} days; not worth an AI call."
    else:
        try:
            raw = ai._call_tool("insight", config.MODEL_INSIGHT, SYSTEM,
                                "<aggregates>\n" + json.dumps(sent, indent=1) + "\n</aggregates>", TOOL, 2500)
            output = verify(raw, sent)
        except Exception as exc:  # the job must never break anything else
            status, note = "failed", re.sub(r"\s+", " ", str(exc))[:300]
    with db.tx() as c:
        return c.execute("""INSERT INTO ai_insights (created_at, model, status, note, input, output)
                            VALUES (?,?,?,?,?,?)""", (db.now(), config.MODEL_INSIGHT, status, note, json.dumps(sent),
                                                      json.dumps(output) if output else None)).lastrowid


def suggestion_to_proposal(insight_id: int, index: int) -> int | None:
    """A founder chose to act on a suggestion: it becomes a normal proposal, still to be approved."""
    row = db.get().execute("SELECT * FROM ai_insights WHERE id=? AND status='ok'", (insight_id,)).fetchone()
    if row is None:
        return None
    suggestions = json.loads(row["output"] or "{}").get("suggestions", [])
    if not 0 <= index < len(suggestions):
        return None
    s = suggestions[index]
    direction = {"raise": 1, "lower": -1}.get(s["direction"])
    if direction is None:
        return None
    key = s["weight_key"]
    sent = json.loads(row["input"])
    scope = sent["event_types"].get(key[5:]) if key.startswith("base.") else sent["rules"].get(key)
    rationale = f"AI insight #{insight_id}{'' if s.get('verified') else ' (numbers NOT verified)'}: {s['rationale']}"
    return learning.create_proposal(key, direction, rationale,
                                    {"scope": "ai_insight", "insight_id": insight_id, "cited_numbers": s.get("numbers"),
                                     "verified": s.get("verified"), "metrics": scope or {}}, source="ai_insight")
