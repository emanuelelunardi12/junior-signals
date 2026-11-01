"""The two red lines, enforced in code on every signal before it goes live.

1. Every published signal carries a primary-source link (http/https) and at least one verbatim quote
   that is literally found in the source document.
2. Nothing we publish is buy/sell advice.

These are guarantees, not a review step: they are deterministic checks that run in milliseconds inside the
automatic alert path (publish.go_live). No human sits here. A signal that fails is withheld automatically
and listed in /admin for information; it is never delayed waiting for someone.
"""
import re
from urllib.parse import urlparse

from app import ai
from app.scoring import EVENT_TYPES

# Phrases that turn a description into a recommendation. Matched on OUR words (headline and extracted
# values), not on verbatim quotes, which are attributed to the source. Kept narrow on purpose: "Big Copper buys
# a 9.9% stake" or "bought deal" describe a transaction and must not trip the check.
ADVICE_PATTERNS = [
    r"\b(strong\s+)?(buy|sell|hold)\s+(rating|recommendation|signal|alert|now|today)\b",
    r"\b(we|i)\s+(recommend|suggest|advise|think you)\b",
    r"\brecommend(s|ed|ation)?\s+(buying|selling|investors?|that)\b",
    r"\byou\s+(should|must|need\s+to|have\s+to)\b",
    r"\b(should|must)\s+(buy|sell|invest|accumulate|short)\b",
    r"\btime\s+to\s+(buy|sell|get\s+in|get\s+out|load\s+up)\b",
    r"\b(price\s+target|target\s+price)\b",
    r"\b(go|going|get)\s+(long|short)\b",
    r"\b(load\s+up|take\s+profits?|buy\s+the\s+dip|don'?t\s+miss)\b",
    r"\b(guaranteed|can'?t\s+lose|cannot\s+lose|to\s+the\s+moon|moonshot|\d+x\s+potential)\b",
    r"\b(undervalued|overvalued|upside\s+potential|cheap\s+stock|bargain)\b",
    r"\b(stock|shares|price)\s+will\s+(soar|skyrocket|explode|double|surge|rise|rally|go\s+up|fall|drop)\b",
    r"^\W*(buy|sell|short|accumulate|avoid|dump)\b",                 # imperative at the start
    r"\bstrong\s+(buy|sell)\b",
    r"\b(outperform|underperform|overweight|underweight|market\s+perform)\b(\s+(rating|recommendation))?",
    r"\b(rate[sd]?|rating)\s+(it\s+|the\s+stock\s+|\w+\s+)?(an?\s+)?(buy|sell|hold)\b",
    r"\b(up|down)grade[sd]?\s+to\s+(buy|sell|hold|outperform|underperform|overweight|underweight)\b",
    r"\bbuying\s+opportunity\b",
    r"\b(buy|sell|accumulate)\s+\S+\s+(before|ahead\s+of|now)\b",
]
_ADVICE = re.compile("|".join(ADVICE_PATTERNS), re.IGNORECASE)


def advice_phrase(text: str) -> str | None:
    """Returns the first advice-like phrase in text, or None."""
    m = _ADVICE.search(text or "")
    return m.group(0) if m else None


def is_primary_source(url: str | None) -> bool:
    try:
        u = urlparse(url or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and "." in u.netloc


def neutral_headline(ticker: str, event_type: str, fields: dict) -> str:
    """A plain, rule-built headline used when the AI's headline reads as advice."""
    what = EVENT_TYPES.get(event_type, "Event")
    who = str(fields.get("counterparty_name") or fields.get("person_name") or "").strip()
    role = str(fields.get("role_title") or "").strip()
    if who and role:
        text = f"{what} at {ticker}: {who}, {role}"
    elif who:
        text = f"{what} in {ticker} involving {who}"
    else:
        text = f"{what} reported by {ticker}"
    return text.rstrip(". ") + "."


def verified_quotes(fields: dict, doc_text: str) -> list[str]:
    """Quotes stored with the signal that are still found verbatim in the source document."""
    quotes = (fields or {}).get("_quotes") or {}
    return [q for q in quotes.values() if isinstance(q, str) and ai.quote_in_doc(q, doc_text or "")]


def violations(headline: str, fields: dict, url: str | None, doc_text: str) -> list[str]:
    """Every red-line breach for a signal about to go live. Empty list = safe to publish."""
    out = []
    if not is_primary_source(url):
        out.append("No primary-source link (http/https) on the document")
    if not verified_quotes(fields, doc_text):
        out.append("No verbatim quote from the source document")
    texts = [headline] + [str(v) for k, v in (fields or {}).items()
                          if k not in ("_quotes", "headline") and isinstance(v, str)]
    for t in texts:
        phrase = advice_phrase(t)
        if phrase:
            out.append(f"Reads as investment advice: \"{phrase}\"")
            break
    return out
