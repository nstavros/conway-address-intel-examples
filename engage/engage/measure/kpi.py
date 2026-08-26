"""KPI configuration and metric normalization. No brand-specific text
lives here (DESIGN.md purity rule) — brand objectives come from
brands/<slug>/config.yaml's `kpi:` block, which restates that brand's
own already-documented goal (see the header comment of each brand's
config.yaml), never a new strategic decision invented here.

Normalization never fabricates a rate from a missing or zero denominator:
every per-1000 / conversion function returns None, not 0.0 or inf, when it
can't be computed — a None propagates as "not computable" through
diagnosis.py rather than silently reading as "zero performance."""
from __future__ import annotations

# Metric category vocabulary — used to keep "did this reach people" and
# "did this create business outcomes" from being blended into one score.
AWARENESS_METRICS = ("impressions", "reach", "non_follower_reach", "views")
RESONANCE_METRICS = ("watch_time", "completion_rate", "likes", "comments", "shares", "saves", "sentiment")
INTENT_METRICS = ("profile_visits", "link_clicks", "inbound_dms")
BUSINESS_OUTCOME_METRICS = ("qualified_leads", "booked_calls", "pipeline_value", "revenue_attributed")
DATA_QUALITY_FIELDS = ("data_coverage", "attribution_status", "response_time")

METRIC_CATEGORIES = {
    **{m: "awareness" for m in AWARENESS_METRICS},
    **{m: "resonance" for m in RESONANCE_METRICS},
    **{m: "intent" for m in INTENT_METRICS},
    **{m: "business_outcome" for m in BUSINESS_OUTCOME_METRICS},
    **{m: "data_quality" for m in DATA_QUALITY_FIELDS},
}

# Evidence threshold shared by diagnosis labeling (§4) and learning writes
# (§6) — a pattern needs at least this many independent content records
# showing a consistent direction before it's called validated_learning or
# written to durable memory. A configuration value, not a magic number
# buried in logic — change it in one place if the bar needs to move.
MIN_EVIDENCE_THRESHOLD = 3


def kpi_config(ctx) -> dict:
    """Reads brands/<slug>/config.yaml's `kpi:` block. Returns an empty,
    honestly-labeled config if the brand hasn't defined one — never
    fabricates a business objective for a brand that hasn't stated one."""
    return ctx.config.get("kpi") or {"business_objective": "", "key_metrics": []}


def per_1000(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator) * 1000


def engagement_per_1000_reach(m: dict) -> float | None:
    engagement = None
    parts = [m.get(k) for k in ("likes", "comments", "shares", "saves")]
    present = [p for p in parts if p is not None]
    if present:
        engagement = sum(present)
    return per_1000(engagement, m.get("reach"))


def saves_per_1000_reach(m: dict) -> float | None:
    return per_1000(m.get("saves"), m.get("reach"))


def shares_per_1000_reach(m: dict) -> float | None:
    return per_1000(m.get("shares"), m.get("reach"))


def profile_visits_per_1000_reach(m: dict) -> float | None:
    return per_1000(m.get("profile_visits"), m.get("reach"))


def qualified_leads_per_1000_reach(m: dict) -> float | None:
    return per_1000(m.get("qualified_leads"), m.get("reach"))


def profile_visit_to_lead_conversion(m: dict) -> float | None:
    visits, leads = m.get("profile_visits"), m.get("qualified_leads")
    if not visits or leads is None:
        return None
    return leads / visits


def click_to_qualified_lead_conversion(m: dict) -> float | None:
    clicks, leads = m.get("link_clicks"), m.get("qualified_leads")
    if not clicks or leads is None:
        return None
    return leads / clicks


NORMALIZED_METRICS = {
    "engagement_per_1000_reach": engagement_per_1000_reach,
    "saves_per_1000_reach": saves_per_1000_reach,
    "shares_per_1000_reach": shares_per_1000_reach,
    "profile_visits_per_1000_reach": profile_visits_per_1000_reach,
    "qualified_leads_per_1000_reach": qualified_leads_per_1000_reach,
    "profile_visit_to_lead_conversion": profile_visit_to_lead_conversion,
    "click_to_qualified_lead_conversion": click_to_qualified_lead_conversion,
}


def normalize(m: dict) -> dict:
    """All seven normalized metrics for one performance row's metrics
    dict, each None where the denominator is missing — never guessed."""
    return {name: fn(m) for name, fn in NORMALIZED_METRICS.items()}
