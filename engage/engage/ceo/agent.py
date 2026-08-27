"""Social Media CEO Agent (Phase 4). Planning and read-only analysis only.

It coordinates Planning & Drafting (drafting/), Publishing Operations
(publish/), and Performance Intelligence (measure/) by producing DIRECTIVES
and ESCALATIONS — structured internal records. It has no path to an external
action: this package imports no sink, no publisher, no approval write path,
no credential, and no network/browser module, and that absence is
test-enforced (tests/test_ceo_agent.py structural checks).

The three distinctions it must never blur, and doesn't:
  RECOMMENDATION  — advisory; produced here and by measure/intelligence.py.
                     Carries an explicit "authorization: NONE" statement.
  APPROVED PLAN   — a human decision, recorded in the planning-source
                     registry or in a ContentRecord's approval row. The CEO
                     reads these; it cannot create one.
  PUBLISHABLE     — a ContentRecord at lifecycle 'approved' with a matching
                     approval hash. Only approval/queue.py can produce that,
                     and only publish/operations.py acts on it.

It cannot bypass any existing gate because it never calls one: to reach
publishing, work still goes through drafting -> gates -> human approval ->
Publishing Operations, exactly as before. A CEO directive is an instruction
to a HUMAN OR AGENT to do that work, not a substitute for it."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import new_id
from ..core.store import Store
from ..drafting.originals import eligible_platforms
from ..measure import kpi

# What the CEO may decide on its own — no human needed.
INTERNAL_DECISIONS = (
    "prioritization", "assignment", "report_generation",
    "low_risk_experiment_recommendation", "inventory_gap_alert", "conflict_identification",
)

# What must go to the owner. Anything not on the internal list lands here.
ESCALATION_TRIGGERS = (
    "content_requiring_human_approval", "real_account_connection", "live_publishing",
    "browser_automation", "comments_or_dms", "paid_promotion",
    "financial_legal_or_regulated_claim", "partnership", "brand_or_reputation_risk",
    "security_mfa_or_account_issue", "budget", "approved_strategy_change",
    "unresolved_material_conflict",
)

OWNER_AGENTS = ("planning_drafting", "publishing_operations", "performance_intelligence", "engagement_future")

CONFLICT_KINDS = (
    "duplicate_campaign", "cross_brand_contamination", "incompatible_account_settings",
    "missing_approval", "missing_performance_data", "paused_or_failed_publishing",
    "disabled_or_unconnected_target", "strategy_plan_mismatch",
)

REQUIRED_DIRECTIVE_FIELDS = (
    "brand", "business_objective", "audience", "platform_scope", "campaign",
    "owner_agent", "deliverable", "deadline", "success_metric",
    "approval_requirement", "exclusions",
)

NO_AUTHORIZATION = (
    "NONE — this is an internal planning directive. It does not authorize "
    "publishing, scheduling, commenting, messaging, connecting an account, "
    "spending money, or changing any account setting. Work described here "
    "still passes every existing gate: drafting -> safety gates -> human "
    "approval -> Publishing Operations validation."
)


class DirectiveRejected(Exception):
    pass


class EscalationRejected(Exception):
    pass


def _canonical(registry: Registry, ctx: BrandContext) -> str:
    return registry.canonical_name(ctx.name)


def business_objective(ctx: BrandContext, registry: Registry) -> dict:
    """Reads the brand's objective from its approved planning source AND its
    config. Returns both plus a `planning_source_resolved` flag — the CEO
    never silently substitutes config for an approved plan when the plan is
    missing; it reports the gap."""
    source = registry.planning_source(ctx.name)
    conf = kpi.kpi_config(ctx)
    return {
        "brand": ctx.name,
        "business_objective": conf.get("business_objective", ""),
        "key_metrics": conf.get("key_metrics", []),
        "targets": conf.get("targets", {}),
        "decision_gates": conf.get("decision_gates", []),
        "planning_source_resolved": source is not None,
        "planning_source": {
            "title": source.get("title"), "location": source.get("location"),
            "updated": source.get("updated"),
        } if source else None,
        "pending_proposals": [
            {"title": p.get("title"), "status": p.get("status")}
            for p in registry.proposals_for(ctx.name)
        ],
    }


def create_directive(store: Store, ctx: BrandContext, registry: Registry, *,
                     business_objective: str, audience: str, platform_scope: list[str],
                     campaign: str, owner_agent: str, deliverable: str, deadline: str,
                     success_metric: str, approval_requirement: str, exclusions: list[str],
                     traces_to: list[str] | None = None, actor: str = "ceo_agent") -> dict:
    """A structured internal directive. Every required field must be present
    and non-empty — an under-specified directive is refused, not filled in
    with a guess. `traces_to` carries content-record / experiment /
    recommendation ids so a directive is always traceable to the underlying
    records it came from."""
    if owner_agent not in OWNER_AGENTS:
        raise DirectiveRejected(f"unknown owner_agent {owner_agent!r} (expected one of {OWNER_AGENTS})")
    if registry.is_parked(ctx.name):
        raise DirectiveRejected(f"'{ctx.name}' is parked — no directives may target it")

    eligible = eligible_platforms(registry, ctx)
    bad = [p for p in platform_scope if p not in eligible]
    if bad:
        raise DirectiveRejected(
            f"platform(s) {bad} not eligible for {ctx.name!r} — disabled, unconnected, "
            f"shared-account, or non-applicable (eligible: {eligible})"
        )

    directive = {
        "id": new_id(),
        "kind": "ceo_directive",
        "brand": ctx.name,
        "brand_canonical": _canonical(registry, ctx),
        "business_objective": business_objective,
        "audience": audience,
        "platform_scope": platform_scope,
        "campaign": campaign,
        "owner_agent": owner_agent,
        "deliverable": deliverable,
        "deadline": deadline,
        "success_metric": success_metric,
        "approval_requirement": approval_requirement,
        "exclusions": exclusions,
        "traces_to": traces_to or [],
        "authorization": NO_AUTHORIZATION,
        "created_at": int(time.time()),
        "actor": actor,
    }
    missing = [f for f in REQUIRED_DIRECTIVE_FIELDS
               if not directive.get(f) and directive.get(f) != 0]
    if missing:
        raise DirectiveRejected(f"directive is missing required field(s): {sorted(missing)}")

    store.save_recommendation({
        "id": directive["id"], "brand": ctx.name, "target": _agent_to_target(owner_agent),
        "label": "observation", "summary": f"[CEO DIRECTIVE] {deliverable}",
        "evidence": directive, "confidence": "n/a", "cross_brand": False,
        "authorization": NO_AUTHORIZATION, "created_at": directive["created_at"],
    })
    return directive


def _agent_to_target(owner_agent: str) -> str:
    return {
        "planning_drafting": "drafting", "publishing_operations": "publishing",
        "performance_intelligence": "ceo", "engagement_future": "engagement",
    }[owner_agent]


def create_escalation(store: Store, ctx: BrandContext, registry: Registry, *,
                      trigger: str, issue: str, why_it_matters: str,
                      recommended_action: str, alternatives: list[str], deadline: str,
                      consequence_of_no_decision: str, traces_to: list[str] | None = None,
                      actor: str = "ceo_agent") -> dict:
    """An owner escalation. Refuses a trigger the CEO is allowed to decide
    internally — escalating a routine call would train the owner to ignore
    escalations, which is the failure mode this agent exists to prevent."""
    if trigger in INTERNAL_DECISIONS:
        raise EscalationRejected(
            f"{trigger!r} is an internal CEO decision, not an owner escalation — "
            f"decide it (internal decisions: {INTERNAL_DECISIONS})"
        )
    if trigger not in ESCALATION_TRIGGERS:
        raise EscalationRejected(f"unknown escalation trigger {trigger!r}")
    for name, value in (("issue", issue), ("why_it_matters", why_it_matters),
                        ("recommended_action", recommended_action), ("deadline", deadline),
                        ("consequence_of_no_decision", consequence_of_no_decision)):
        if not (value or "").strip():
            raise EscalationRejected(f"escalation is missing required field: {name}")

    escalation = {
        "id": new_id(), "kind": "owner_escalation", "brand": ctx.name,
        "brand_canonical": _canonical(registry, ctx), "trigger": trigger, "issue": issue,
        "why_it_matters": why_it_matters, "recommended_action": recommended_action,
        "alternatives": alternatives, "deadline": deadline,
        "consequence_of_no_decision": consequence_of_no_decision,
        "traces_to": traces_to or [], "authorization": NO_AUTHORIZATION,
        "created_at": int(time.time()), "actor": actor,
    }
    store.save_recommendation({
        "id": escalation["id"], "brand": ctx.name, "target": "ceo",
        "label": "observation", "summary": f"[OWNER ESCALATION] {issue}",
        "evidence": escalation, "confidence": "n/a", "cross_brand": False,
        "authorization": NO_AUTHORIZATION, "created_at": escalation["created_at"],
    })
    return escalation


def detect_conflicts(store: Store, ctx: BrandContext, registry: Registry) -> list[dict]:
    """Read-only conflict scan across the existing records. Every conflict
    carries the ids it was derived from, so nothing is unfalsifiable."""
    conflicts: list[dict] = []

    def add(kind: str, detail: str, ids: list[str], escalate: bool = False):
        conflicts.append({"kind": kind, "brand": ctx.name, "detail": detail,
                          "traces_to": ids, "requires_escalation": escalate})

    if registry.planning_source(ctx.name) is None and not registry.is_parked(ctx.name):
        add("strategy_plan_mismatch",
            f"no approved planning source recorded for active brand {ctx.name!r} — "
            "strategy cannot be verified against a source of truth", [], escalate=True)

    for p in registry.proposals_for(ctx.name):
        if p.get("status") == "awaiting_owner_go":
            add("strategy_plan_mismatch",
                f"pending proposal awaiting your go: {p.get('title')} — not authoritative "
                "until approved, so nothing in it is scheduled or counted", [], escalate=True)

    records = store.list_content_records(ctx.name)
    eligible = eligible_platforms(registry, ctx)

    for r in records:
        if r.platform not in eligible and r.lifecycle_status not in ("rejected", "cancelled"):
            add("disabled_or_unconnected_target",
                f"content record {r.id} targets {r.platform!r}, not currently eligible "
                f"(eligible: {eligible})", [r.id], escalate=True)

    # Must use the dedicated foreign-brand query, NOT the loop above:
    # list_content_records() filters WHERE brand=?, so a contaminated row is
    # invisible to it and a check written against it would be dead code.
    foreign = store.foreign_brand_content_records(ctx.name)
    if foreign:
        add("cross_brand_contamination",
            f"{len(foreign)} content record(s) in {ctx.name!r}'s database carry a different "
            "brand — one DB per brand is the isolation boundary, so this is contamination at rest",
            foreign, escalate=True)

    paused = [r.id for r in records if r.lifecycle_status == "paused"]
    if paused:
        add("paused_or_failed_publishing", f"{len(paused)} record(s) paused pending review", paused)
    failed = [r.id for r in records if r.lifecycle_status == "failed"]
    if failed:
        add("paused_or_failed_publishing", f"{len(failed)} record(s) in failed state", failed, escalate=True)

    awaiting = [r.id for r in records if r.lifecycle_status == "review_required"]
    if awaiting:
        add("missing_approval", f"{len(awaiting)} draft(s) awaiting your approval", awaiting, escalate=True)

    published = [r for r in records if r.lifecycle_status in ("published", "verified")]
    measured = {p.content_record_id for p in store.list_performance_records(ctx.name)}
    unmeasured = [r.id for r in published if r.id not in measured]
    if unmeasured:
        add("missing_performance_data",
            f"{len(unmeasured)} published item(s) have no performance data — "
            "missing data, NOT evidence of poor performance", unmeasured)

    seen: dict[tuple, str] = {}
    for r in records:
        if r.lifecycle_status in ("rejected", "cancelled"):
            continue
        key = (r.platform, (r.brief or {}).get("caption", "")[:120])
        if key[1] and key in seen:
            add("duplicate_campaign",
                f"content record {r.id} duplicates {seen[key]} on {r.platform}", [r.id, seen[key]])
        elif key[1]:
            seen[key] = r.id

    return conflicts
