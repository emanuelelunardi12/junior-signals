"""The learning loop: the system proposes changes to its own scoring; a founder decides.

    INPUT        implicit behaviour per event type and per scoring rule (opens vs impressions, scroll-past,
                 source clicks, dwell, Useful / Not useful) from app/behaviour.py, plus the rules themselves.
    LEARNING     run_cycle() aggregates it deterministically and turns consistent patterns into PROPOSED
                 weight changes (+1 or -1), each with the numbers behind it.
    HUMAN CHECK  approve() is the ONLY code path that changes a weight. Nothing here auto-applies.
    IMPROVEMENT  once approved, every signal scored afterwards uses the new weight (and is stamped with the
                 weights version), so the next alerts of that type are scored and tiered better.
    MEASUREMENT  each cycle records engagement per event type and headline metrics; each approved change
                 records a before snapshot and is compared with behaviour on signals scored after it.

Why the human sits HERE and not in the alert path: publishing one fast alert is safe (it is quote-checked and
source-linked, see app/guardrails.py). The risky step is the system silently rewiring how it scores everything
from noisy engagement data: overnight drift, rewarding clickbait, losing the ability to explain a score.

Built-in safeguards, all in code:
- minimum evidence before any proposal (MIN_IMPRESSIONS, MIN_RATINGS);
- a raise needs users to LIKE the signals, not just click them (engagement alone never raises a weight);
- one step at a time, within scoring bounds, and at most MAX_DRIFT away from the code default;
- one open proposal per weight; a proposal is refused at approval if the weight changed since it was made;
- every change is an append-only row in weight_changes and can be reverted.
"""
import json
from datetime import datetime, timedelta, timezone

from app import behaviour, db, scoring

WINDOW_DAYS = 30
MIN_IMPRESSIONS = 30      # web impressions + Telegram deliveries before an event type can be judged
MIN_RATINGS = 5           # Useful / Not useful ratings before like rate counts
MIN_RULE_RATINGS = 8      # stricter for rules: they overlap with event types
LIKE_HIGH, LIKE_LOW = 0.70, 0.35
ENGAGED = 1.2             # open rate or CTR at least 20% above the average of all types
IGNORED_OPEN = 0.6        # open rate at most 60% of average ...
IGNORED_SKIP = 0.6        # ... and at least 60% of impressions scrolled past
STEP = 1
MAX_DRIFT = 2             # learning may move a weight at most this far from the code default


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---------------- weights in force ----------------

def weights_version() -> int:
    row = db.get().execute("SELECT MAX(id) v FROM weight_changes").fetchone()
    return row["v"] or 0


def effective_weights() -> dict:
    """Code defaults with every founder-approved change applied (latest change per weight wins)."""
    w = scoring.default_weights()
    rows = db.get().execute("""SELECT weight_key, new_value FROM weight_changes
                               WHERE id IN (SELECT MAX(id) FROM weight_changes GROUP BY weight_key)""")
    for r in rows:
        if r["weight_key"] in scoring.RULE_LABELS:
            scoring.set_weight(w, r["weight_key"], r["new_value"])
    return w


def weight_table() -> list[dict]:
    """Every tunable weight: code default, value in force, and the last change."""
    default, now = scoring.default_weights(), effective_weights()
    last = {r["weight_key"]: r for r in db.get().execute(
        "SELECT * FROM weight_changes WHERE id IN (SELECT MAX(id) FROM weight_changes GROUP BY weight_key)")}
    return [{"key": k, "label": label, "default": scoring.get_weight(default, k), "value": scoring.get_weight(now, k),
             "last": last.get(k)} for k, label in scoring.RULE_LABELS.items()]


# ---------------- aggregation ----------------

def engagement_by_rule(since: str | None = None, min_weights_version: int | None = None) -> dict[str, dict]:
    """Like rate, open rate and CTR of signals whose score included each rule."""
    c = db.get()
    ver = " AND s.weights_version >= ?" if min_weights_version is not None else ""
    vargs = [min_weights_version] if min_weights_version is not None else []
    since_b = " AND b.created_at >= ?" if since else ""
    since_f = " AND f.created_at >= ?" if since else ""
    sargs = [since] if since else []
    out: dict[str, dict] = {}

    def slot(k):
        return out.setdefault(k, {"impressions": 0, "opens": 0, "source_clicks": 0, "scroll_pasts": 0,
                                  "likes": 0, "dislikes": 0})
    key = {"impression": "impressions", "open": "opens", "source_click": "source_clicks", "scroll_past": "scroll_pasts"}
    for r in c.execute(f"""
            SELECT j.value AS rule, b.action, COUNT(DISTINCT b.session_id || ':' || b.signal_id) AS pairs
            FROM behaviour_events b JOIN signals s ON s.id = b.signal_id, json_each(s.rule_keys) j
            WHERE b.surface='web' AND b.action IN ('impression','open','source_click','scroll_past') {since_b} {ver}
            GROUP BY j.value, b.action""", sargs + vargs):
        slot(r["rule"])[key[r["action"]]] = r["pairs"]
    for r in c.execute(f"""
            SELECT j.value AS rule, SUM(f.useful) up, COUNT(*) n
            FROM feedback f JOIN signals s ON s.id = f.signal_id, json_each(s.rule_keys) j
            WHERE 1=1 {since_f} {ver} GROUP BY j.value""", sargs + vargs):
        slot(r["rule"])["likes"] = r["up"] or 0
        slot(r["rule"])["dislikes"] = r["n"] - (r["up"] or 0)
    for m in out.values():
        m["ratings"] = m["likes"] + m["dislikes"]
        m["like_rate"] = behaviour._ratio(m["likes"], m["ratings"])
        m["open_rate"] = behaviour._ratio(m["opens"], m["impressions"])
        m["ctr"] = behaviour._ratio(m["source_clicks"], m["impressions"])
        m["skip_rate"] = behaviour._ratio(m["scroll_pasts"], m["impressions"])
    return {k: v for k, v in out.items() if k in scoring.RULE_LABELS}


def _averages(metrics: dict[str, dict]) -> dict:
    tot = {k: sum(m[k] for m in metrics.values()) for k in ("impressions", "opens", "source_clicks", "scroll_pasts")}
    return {"open_rate": behaviour._ratio(tot["opens"], tot["impressions"]),
            "ctr": behaviour._ratio(tot["source_clicks"], tot["impressions"]),
            "skip_rate": behaviour._ratio(tot["scroll_pasts"], tot["impressions"])}


def headline_metrics(since: str | None = None, min_weights_version: int | None = None) -> dict:
    """The learning metrics we track over time: how the alert tier performs, and CTR on the top of the feed."""
    c = db.get()
    since_b = " AND b.created_at >= ?" if since else ""
    since_f = " AND f.created_at >= ?" if since else ""
    ver = " AND s.weights_version >= ?" if min_weights_version is not None else ""
    args = ([since] if since else []) + ([min_weights_version] if min_weights_version is not None else [])

    def pairs(action, extra=""):
        return c.execute(f"""SELECT COUNT(DISTINCT b.session_id || ':' || b.signal_id) n FROM behaviour_events b
                             JOIN signals s ON s.id = b.signal_id
                             WHERE b.surface='web' AND b.action=? {extra} {since_b} {ver}""", [action] + args).fetchone()["n"]
    alert = " AND s.tier='alert'"
    imp, opn = pairs("impression", alert), pairs("open", alert)
    rated = c.execute(f"""SELECT SUM(f.useful) up, COUNT(*) n FROM feedback f JOIN signals s ON s.id=f.signal_id
                          WHERE s.tier='alert' {since_f} {ver}""", args).fetchone()
    top = c.execute(f"""SELECT b.session_id || ':' || b.signal_id AS k FROM behaviour_events b
                        JOIN signals s ON s.id = b.signal_id
                        WHERE b.surface='web' AND b.action='impression' AND b.position < 3 {since_b} {ver}""",
                    args).fetchall()
    top_keys = {r["k"] for r in top}
    clicks = {r["k"] for r in c.execute(f"""SELECT b.session_id || ':' || b.signal_id AS k FROM behaviour_events b
                        JOIN signals s ON s.id = b.signal_id
                        WHERE b.surface='web' AND b.action='source_click' {since_b} {ver}""", args)}
    return {
        "alert_impressions": imp,
        "alert_open_rate": behaviour._ratio(opn, imp),
        "alert_ratings": rated["n"] or 0,
        "alert_like_rate": behaviour._ratio(rated["up"] or 0, rated["n"] or 0),
        "top3_impressions": len(top_keys),
        "top3_ctr": behaviour._ratio(len(top_keys & clicks), len(top_keys)),
    }


# ---------------- proposals ----------------

def _judge(m: dict, avg: dict, min_ratings: int) -> tuple[int, str] | None:
    """Deterministic rule: +1 if consistently liked AND engaged, -1 if consistently disliked or ignored."""
    liked = m["ratings"] >= min_ratings and m["like_rate"] is not None and m["like_rate"] >= LIKE_HIGH
    disliked = m["ratings"] >= min_ratings and m["like_rate"] is not None and m["like_rate"] <= LIKE_LOW
    engaged = m["impressions"] >= MIN_IMPRESSIONS and (
        (avg["open_rate"] and (m["open_rate"] or 0) >= ENGAGED * avg["open_rate"]) or
        (avg["ctr"] and (m["ctr"] or 0) >= ENGAGED * avg["ctr"]))
    ignored = m["impressions"] >= MIN_IMPRESSIONS and avg["open_rate"] and (m["skip_rate"] or 0) >= IGNORED_SKIP \
        and (m["open_rate"] or 0) <= IGNORED_OPEN * avg["open_rate"]
    pct = lambda x: "n/a" if x is None else f"{x * 100:.0f}%"
    if liked and engaged:
        return +STEP, (f"Users rated {m['likes']} of {m['ratings']} useful ({pct(m['like_rate'])}) and engage more than "
                       f"average (open rate {pct(m['open_rate'])} vs {pct(avg['open_rate'])}, click-through "
                       f"{pct(m['ctr'])} vs {pct(avg['ctr'])}).")
    if disliked:
        return -STEP, f"Users rated only {m['likes']} of {m['ratings']} useful ({pct(m['like_rate'])})."
    if ignored:
        return -STEP, (f"Consistently ignored: {pct(m['skip_rate'])} of {m['impressions']} impressions scrolled past, "
                       f"open rate {pct(m['open_rate'])} vs {pct(avg['open_rate'])} on average.")
    return None  # includes "liked but not engaged" and "engaged but not liked": no change on thin or mixed evidence


def _allowed(key: str, current: int, proposed: int) -> bool:
    lo, hi = scoring.bounds(key)
    default = scoring.get_weight(scoring.default_weights(), key)
    return lo <= proposed <= hi and abs(proposed - default) <= MAX_DRIFT and proposed != current


def _open_proposal(key: str):
    return db.get().execute("SELECT 1 FROM tuning_proposals WHERE weight_key=? AND status='proposed'", (key,)).fetchone()


def create_proposal(key: str, direction: int, rationale: str, evidence: dict, source: str = "behaviour",
                    cycle_id: int | None = None) -> int | None:
    """Record a PROPOSED change. Does not touch the weights. Returns the proposal id, or None if not allowed."""
    if key not in scoring.RULE_LABELS or direction not in (-1, 1) or _open_proposal(key):
        return None
    current = scoring.get_weight(effective_weights(), key)
    proposed = current + direction * STEP
    if not _allowed(key, current, proposed):
        return None
    with db.tx() as c:
        cur = c.execute("""INSERT INTO tuning_proposals (created_at, cycle_id, source, weight_key, current_value,
                           proposed_value, rationale, evidence) VALUES (?,?,?,?,?,?,?,?)""",
                        (db.now(), cycle_id, source, key, current, proposed, rationale[:1000], json.dumps(evidence)))
        return cur.lastrowid


def run_cycle(window_days: int = WINDOW_DAYS) -> dict:
    """One learning cycle. Aggregates behaviour, records metrics, and PROPOSES changes. Never applies one."""
    since = _ago(window_days)
    by_type = behaviour.engagement_by_event_type(since=since)
    by_rule = engagement_by_rule(since=since)
    avg = _averages(by_type)
    metrics = headline_metrics(since=since)
    metrics["weights_version"] = weights_version()
    metrics["averages"] = avg
    events = db.get().execute("SELECT COUNT(*) n FROM behaviour_events WHERE created_at >= ?", (since,)).fetchone()["n"]
    with db.tx() as c:
        cycle_id = c.execute("""INSERT INTO learning_cycles (ran_at, window_days, events, proposals, metrics)
                                VALUES (?,?,?,0,?)""", (db.now(), window_days, events, json.dumps(metrics))).lastrowid
        for et, m in by_type.items():
            c.execute("""INSERT INTO engagement_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (cycle_id, et, m["impressions"], m["opens"], m["source_clicks"], m["scroll_pasts"],
                       m["telegram_deliveries"], m["likes"], m["dislikes"], m["open_rate"], m["like_rate"],
                       m["ctr"], m["skip_rate"], m["avg_dwell"]))
    made = []
    for et, m in by_type.items():
        verdict = _judge(m, avg, MIN_RATINGS)
        if verdict:
            pid = create_proposal(f"base.{et}", verdict[0], verdict[1],
                                  {"scope": "event_type", "event_type": et, "window_days": window_days,
                                   "metrics": m, "averages": avg}, cycle_id=cycle_id)
            made += [pid] if pid else []
    for rule, m in by_rule.items():
        if rule.startswith("base."):
            continue  # base weights are judged per event type above
        m.setdefault("telegram_deliveries", 0)
        verdict = _judge(m, avg, MIN_RULE_RATINGS)
        if verdict:
            pid = create_proposal(rule, verdict[0], verdict[1],
                                  {"scope": "rule", "rule": rule, "window_days": window_days,
                                   "metrics": m, "averages": avg}, cycle_id=cycle_id)
            made += [pid] if pid else []
    with db.tx() as c:
        c.execute("UPDATE learning_cycles SET proposals=? WHERE id=?", (len(made), cycle_id))
    purged = behaviour.purge_older_than()
    return {"cycle_id": cycle_id, "events": events, "proposals": made, "purged": purged}


# ---------------- the human safety check ----------------

class NotAllowed(Exception):
    pass


def target_metrics(key: str, since: str | None = None, min_weights_version: int | None = None) -> dict | None:
    """Engagement for whatever a weight affects: its event type, or the signals carrying its rule."""
    if key.startswith("base."):
        return behaviour.engagement_by_event_type(since=since, min_weights_version=min_weights_version).get(key[5:])
    return engagement_by_rule(since=since, min_weights_version=min_weights_version).get(key)


def approve(proposal_id: int, admin_id: int, note: str = "") -> int:
    """HUMAN SAFETY CHECK. The only function in the codebase that changes how signals are scored.

    Called from /admin/tuning by a founder (ADMIN_IDS) after reading the evidence. Records a before snapshot,
    appends the change to weight_changes, and returns the new weights version. Signals already published keep
    the score they were given; signals scored from now on use the new weight.
    """
    p = db.get().execute("SELECT * FROM tuning_proposals WHERE id=?", (proposal_id,)).fetchone()
    if p is None or p["status"] != "proposed":
        raise NotAllowed("That proposal is not open.")
    current = scoring.get_weight(effective_weights(), p["weight_key"])
    if current != p["current_value"]:
        with db.tx() as c:
            c.execute("UPDATE tuning_proposals SET status='superseded', decided_at=? WHERE id=?", (db.now(), proposal_id))
        raise NotAllowed("The weight changed since this was proposed; it has been marked superseded.")
    if not _allowed(p["weight_key"], current, p["proposed_value"]):
        raise NotAllowed("That value is outside the allowed range.")
    before = {"target": target_metrics(p["weight_key"], since=_ago(WINDOW_DAYS)),
              "headline": headline_metrics(since=_ago(WINDOW_DAYS)), "taken_at": db.now()}
    with db.tx() as c:
        version = c.execute("""INSERT INTO weight_changes (weight_key, old_value, new_value, proposal_id, kind,
                               changed_by, changed_at) VALUES (?,?,?,?,'approve',?,?)""",
                            (p["weight_key"], current, p["proposed_value"], proposal_id, admin_id, db.now())).lastrowid
        c.execute("""UPDATE tuning_proposals SET status='approved', decided_by=?, decided_at=?, decision_note=?,
                     before_metric=?, applied_version=? WHERE id=?""",
                  (admin_id, db.now(), note[:500], json.dumps(before), version, proposal_id))
    return version


def reject(proposal_id: int, admin_id: int, note: str = "") -> None:
    with db.tx() as c:
        cur = c.execute("""UPDATE tuning_proposals SET status='rejected', decided_by=?, decided_at=?, decision_note=?
                           WHERE id=? AND status='proposed'""", (admin_id, db.now(), note[:500], proposal_id))
    if not cur.rowcount:
        raise NotAllowed("That proposal is not open.")


def revert(proposal_id: int, admin_id: int) -> int:
    """Undo an approved change (also a founder decision). Appends a new row; history is never rewritten."""
    p = db.get().execute("SELECT * FROM tuning_proposals WHERE id=? AND status='approved'", (proposal_id,)).fetchone()
    if p is None:
        raise NotAllowed("Only an approved change can be reverted.")
    current = scoring.get_weight(effective_weights(), p["weight_key"])
    if current != p["proposed_value"]:
        raise NotAllowed("This weight has changed again since; revert the later change first.")
    with db.tx() as c:
        version = c.execute("""INSERT INTO weight_changes (weight_key, old_value, new_value, proposal_id, kind,
                               changed_by, changed_at) VALUES (?,?,?,?,'revert',?,?)""",
                            (p["weight_key"], current, p["current_value"], proposal_id, admin_id, db.now())).lastrowid
        c.execute("UPDATE tuning_proposals SET status='reverted' WHERE id=?", (proposal_id,))
    return version


def before_after(proposal) -> dict:
    """The measurable result of an approved change: target engagement before vs on signals scored after it."""
    before = json.loads(proposal["before_metric"] or "{}")
    after_target = target_metrics(proposal["weight_key"], min_weights_version=proposal["applied_version"])
    after_head = headline_metrics(min_weights_version=proposal["applied_version"])
    return {"before": before.get("target") or {}, "after": after_target or {},
            "before_head": before.get("headline") or {}, "after_head": after_head}
