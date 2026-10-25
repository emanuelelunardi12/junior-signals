"""Transparent, rule-based signal scoring.

Every point is explained in the breakdown shown to users. The weights below are STARTING GUESSES:
replace them with evidence from the retro-signal study and from user feedback ("Useful / Not useful").
Keep all tunable numbers in WEIGHTS / THRESHOLDS so changes are one-line and easy to log.
"""
from dataclasses import dataclass, field

EVENT_TYPES = {
    "strategic_investment": "Strategic investment",
    "ownership_threshold": "Ownership threshold crossed",
    "key_hire_ma": "M&A-type hire",
    "key_hire_development": "Development-stage hire",
    "private_placement": "Private placement",
}

WEIGHTS = {
    # base points by event type
    "base": {
        "strategic_investment": 5,
        "ownership_threshold": 4,
        "key_hire_ma": 3,
        "key_hire_development": 2,
        "private_placement": 1,
    },
    "strategic_counterparty": 1,      # extra if the investor is a major producer or royalty/streaming co.
    "premium_to_market": 2,
    "discount_with_full_warrants": -1,
    "large_vs_market_cap": 1,
    "governance_rights": 2,            # board seat, top-up / participation rights, or standstill
    "just_under_control": 1,
    "first_time_holder": 1,
    "already_moved": -2,
    "cluster_per_signal": 2,
    "cluster_max": 4,
}

THRESHOLDS = {
    "large_vs_market_cap_pct": 10.0,   # deal size above this % of market cap
    "control_band": (19.0, 20.0),      # stake just below a 20% control threshold
    "already_moved_pct": 20.0,         # price up this much in the prior days (needs price data)
    "cluster_days": 60,
    "tier_alert": 8,
    "tier_feed": 4,
}

STRATEGIC_TYPES = {"major_producer", "royalty_streaming"}


@dataclass
class Context:
    """Facts that come from our database, not from the document."""
    market_cap_musd: float | None = None
    first_time_holder: bool = False
    recent_signal_count: int = 0          # other kept signals on this ticker within cluster_days
    prior_move_pct: float | None = None   # TODO: fill once we add a price source


@dataclass
class Result:
    score: int
    tier: str
    breakdown: list[tuple[str, int]] = field(default_factory=list)


def tier_for(score: int) -> str:
    if score >= THRESHOLDS["tier_alert"]:
        return "alert"
    if score >= THRESHOLDS["tier_feed"]:
        return "feed"
    return "archive"


def score_signal(event_type: str, f: dict, ctx: Context) -> Result:
    """f is a flat dict of verified field values: {field_name: value}."""
    W, T = WEIGHTS, THRESHOLDS
    parts: list[tuple[str, int]] = []

    base = W["base"].get(event_type)
    if base is None:
        return Result(0, "archive", [("Not a tracked event type", 0)])
    parts.append((EVENT_TYPES[event_type], base))

    if f.get("counterparty_type") in STRATEGIC_TYPES:
        label = "Investor is a major producer" if f["counterparty_type"] == "major_producer" \
            else "Investor is a royalty or streaming company"
        parts.append((label, W["strategic_counterparty"]))

    price = f.get("price_vs_market")
    if price == "premium":
        parts.append(("Priced at a premium to market", W["premium_to_market"]))
    elif price == "discount" and f.get("full_warrants") is True:
        parts.append(("Discount with full warrants (routine dilution)", W["discount_with_full_warrants"]))

    amount = f.get("amount_musd")
    if amount and ctx.market_cap_musd:
        pct = 100.0 * float(amount) / float(ctx.market_cap_musd)
        if pct > T["large_vs_market_cap_pct"]:
            parts.append((f"Size is {pct:.0f}% of market cap", W["large_vs_market_cap"]))

    if any(f.get(k) is True for k in ("board_seat", "topup_rights", "standstill")):
        parts.append(("Investor gets board seat, top-up rights or standstill", W["governance_rights"]))

    stake = f.get("stake_pct")
    lo, hi = T["control_band"]
    if stake is not None and lo <= float(stake) < hi:
        parts.append((f"Stake of {float(stake):g}% sits just under 20%", W["just_under_control"]))

    if ctx.first_time_holder and f.get("counterparty_name"):
        parts.append(("First time this investor appears", W["first_time_holder"]))

    if ctx.prior_move_pct is not None and ctx.prior_move_pct >= T["already_moved_pct"]:
        parts.append((f"Stock already up {ctx.prior_move_pct:.0f}% (likely priced in)", W["already_moved"]))

    if ctx.recent_signal_count > 0:
        bonus = min(W["cluster_per_signal"] * ctx.recent_signal_count, W["cluster_max"])
        n = ctx.recent_signal_count
        parts.append((f"{n} other signal{'s' if n > 1 else ''} in the last {T['cluster_days']} days", bonus))

    total = sum(p for _, p in parts)
    return Result(total, tier_for(total), parts)
