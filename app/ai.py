"""The three AI steps: classify -> extract (Maker) -> grade (Grader).

Design rules (keep these when editing):
- Every extracted field must carry a verbatim quote from the document.
- A field is only kept if (1) its quote is literally found in the document AND (2) the Grader
  confirms the quote supports the value. Step (1) is plain code: free and never hallucinates.
- Models only return structured data through forced tool calls; no free-text parsing.
- All prompts live in this file. Log every prompt change in docs/prompt_log.md (AI Orchestration Log).
"""
import json
import re
import unicodedata

from app import config, db
from app.scoring import EVENT_TYPES

CLASSES = list(EVENT_TYPES) + ["noise"]

FIELDS = {
    "headline": "One factual sentence describing the event, no opinion, no prediction.",
    "counterparty_name": "Name of the investor, acquirer or holder (if any).",
    "counterparty_type": "One of: major_producer, royalty_streaming, institutional_fund, insider, other.",
    "amount_millions": "Deal size in millions of the stated currency (number).",
    "currency": "ISO code of the deal currency, e.g. CAD, USD, AUD.",
    "price_vs_market": "One of: premium, at_market, discount. Only if the document states it or gives both prices.",
    "full_warrants": "true if each unit includes one full warrant.",
    "stake_pct": "Ownership percentage held after the transaction (number).",
    "board_seat": "true if the investor gets the right to nominate a director.",
    "topup_rights": "true if the investor gets participation or top-up rights.",
    "standstill": "true if a standstill agreement is mentioned.",
    "role_title": "Job title for a hire or appointment.",
    "person_name": "Name of the person hired or appointed.",
    "event_date": "Date of the event as written in the document.",
}

# ---------------- prompts ----------------

CLASSIFY_SYSTEM = """You classify news releases and regulatory filings of junior mining and oil & gas companies.
Choose exactly one class:
- strategic_investment: a mining/energy company, royalty or streaming company buys shares or makes a strategic investment
- ownership_threshold: a holder crosses a disclosure threshold (early warning report, 13D/13G, substantial holder notice)
- key_hire_ma: appointment of VP Corporate Development, business development head, or CFO/CEO with explicit M&A/deal background
- key_hire_development: appointment of mine manager, construction manager, project director, COO for a development project
- private_placement: financing (private placement, bought deal, flow-through) without a strategic investor
- noise: anything else (drill results, conference attendance, marketing, AGM results, option grants...)
Judge only from the text. If unsure between a class and noise, choose noise."""

EXTRACT_SYSTEM = """You extract facts from a news release or filing of a junior resource company.
Rules:
- Only extract fields that the document explicitly states. Omit a field rather than guess.
- For every field, copy a short verbatim quote (one sentence or less) from the document that proves the value.
  The quote must match the document character for character.
- Numbers: plain numbers without units (e.g. 9.9 for 9.9%, 12.5 for $12.5 million).
- The headline is a single neutral sentence; its quote is the sentence that best supports it.
Available fields:
""" + "\n".join(f"- {k}: {v}" for k, v in FIELDS.items())

GRADE_SYSTEM = """You are a strict fact checker. For each extracted field you get the value and the quote
the extractor used. Decide if the quote, read in the context of the full document, clearly supports the value.
Mark supported=false if the value adds anything not stated, misreads a number, confuses parties, or relies
on assumption. Give a one-sentence reason for every false."""

# ---------------- tool schemas ----------------

CLASSIFY_TOOL = {
    "name": "record_class",
    "description": "Record the document class.",
    "input_schema": {
        "type": "object",
        "properties": {
            "event_type": {"type": "string", "enum": CLASSES},
            "reason": {"type": "string"},
        },
        "required": ["event_type", "reason"],
    },
}

EXTRACT_TOOL = {
    "name": "record_fields",
    "description": "Record extracted fields, each with a verbatim supporting quote.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": list(FIELDS)},
                        "value": {"type": ["string", "number", "boolean"]},
                        "quote": {"type": "string"},
                    },
                    "required": ["field", "value", "quote"],
                },
            }
        },
        "required": ["items"],
    },
}

GRADE_TOOL = {
    "name": "record_checks",
    "description": "Record whether each field is supported.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "supported": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["field", "supported"],
                },
            }
        },
        "required": ["checks"],
    },
}

# ---------------- model call ----------------

_client = None


def _client_get():
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _call_tool(stage: str, model: str, system: str, user: str, tool: dict, max_tokens: int = 2000) -> dict:
    """One forced tool call. Returns the tool input dict. Tests monkeypatch this function."""
    resp = _client_get().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    db.log_usage(stage, model, resp.usage.input_tokens, resp.usage.output_tokens)
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError(f"{stage}: model returned no tool call")


def _doc(text: str, title: str = "") -> str:
    return f"<document>\n<title>{title}</title>\n{text[: config.MAX_DOC_CHARS]}\n</document>"


# ---------------- steps ----------------

def classify(text: str, title: str = "") -> str:
    out = _call_tool("classify", config.MODEL_CLASSIFY, CLASSIFY_SYSTEM, _doc(text, title), CLASSIFY_TOOL, 300)
    return out["event_type"] if out.get("event_type") in CLASSES else "noise"


def extract(text: str, title: str, event_type: str, feedback: str = "") -> list[dict]:
    user = _doc(text, title) + f"\n\nThe document was classified as: {event_type}."
    if feedback:
        user += "\n\nA fact checker rejected some of your previous fields. Fix or omit them:\n" + feedback
    out = _call_tool("extract", config.MODEL_EXTRACT, EXTRACT_SYSTEM, user, EXTRACT_TOOL)
    items = out.get("items") or []
    return [i for i in items if i.get("field") in FIELDS]


def grade(text: str, title: str, items: list[dict]) -> dict[str, tuple[bool, str]]:
    if not items:
        return {}
    user = _doc(text, title) + "\n\nExtracted fields:\n" + json.dumps(items, ensure_ascii=False, indent=1)
    out = _call_tool("grade", config.MODEL_GRADE, GRADE_SYSTEM, user, GRADE_TOOL)
    return {c["field"]: (bool(c["supported"]), c.get("reason", "")) for c in out.get("checks", [])}


# ---------------- deterministic quote check ----------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_doc(quote: str, text: str) -> bool:
    q = _norm(quote or "")
    return len(q) >= 8 and q in _norm(text)


# ---------------- Maker and Grader loop ----------------

def extract_verified(text: str, title: str, event_type: str, use_grader: bool = True, retry: bool = True):
    """Returns (kept_items, rejected_items). rejected items carry a 'reason'."""

    def check(items):
        kept, rejected = [], []
        quote_ok = []
        for it in items:
            if quote_in_doc(it.get("quote", ""), text):
                quote_ok.append(it)
            else:
                rejected.append({**it, "reason": "Quote not found in the document"})
        if use_grader:
            verdicts = grade(text, title, quote_ok)
            for it in quote_ok:
                ok, reason = verdicts.get(it["field"], (False, "Grader did not check this field"))
                (kept if ok else rejected).append(it if ok else {**it, "reason": reason})
        else:
            kept = quote_ok
        return kept, rejected

    kept, rejected = check(extract(text, title, event_type))
    if retry and use_grader and rejected:
        feedback = "\n".join(f"- {r['field']}: {r['reason']}" for r in rejected)
        retry_items = extract(text, title, event_type, feedback)
        kept_fields = {k["field"] for k in kept}
        retry_items = [i for i in retry_items if i["field"] not in kept_fields]
        kept2, rejected2 = check(retry_items)
        kept += kept2
        fixed = {k["field"] for k in kept2}
        rejected = [r for r in rejected if r["field"] not in fixed] + \
                   [r for r in rejected2 if r["field"] not in {x["field"] for x in rejected}]
    return kept, rejected
