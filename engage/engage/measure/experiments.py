"""Experiment ledger. Every experiment is a RECOMMENDATION — nothing in
this module ever touches a calendar, draft, schedule, publication, or
account. There is no function here that writes to drafts, content_records,
or approvals; validate_experiment() and create_experiment() only ever
write to the experiments table.

Reuses the existing registry/eligibility checks (config/registry.py,
drafting/originals.eligible_platforms) rather than re-implementing
account-state validation — an experiment can't target an account this
system wouldn't already draft or publish for."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import EXPERIMENT_STATUSES, Experiment, new_id
from ..core.store import Store
from ..drafting.originals import eligible_platforms

REQUIRED_TEXT_FIELDS = (
    "business_objective", "hypothesis", "independent_variable", "control",
    "treatment", "target_platform", "success_metric", "decision_rule",
)


class ExperimentValidationError(Exception):
    pass


def validate_experiment(exp: Experiment, registry: Registry, ctx: BrandContext) -> list[str]:
    """Returns a list of problems (empty = valid). Never raises — callers
    decide whether to reject or just record the problems."""
    problems = []
    for field_name in REQUIRED_TEXT_FIELDS:
        if not (getattr(exp, field_name) or "").strip():
            problems.append(f"missing required field: {field_name}")
    if exp.brand != ctx.name:
        problems.append(f"experiment.brand {exp.brand!r} does not match ctx {ctx.name!r} — "
                        "an experiment must target exactly one brand")
    if registry.is_parked(ctx.name):
        problems.append(f"'{ctx.name}' is parked in the brand registry — no experiments permitted")
    elif exp.target_platform not in eligible_platforms(registry, ctx):
        problems.append(
            f"target_platform {exp.target_platform!r} is not eligible for {ctx.name!r} "
            "(disabled, unconnected, shared-account, or non-applicable)"
        )
    if exp.status not in EXPERIMENT_STATUSES:
        problems.append(f"status must be one of {EXPERIMENT_STATUSES}, got {exp.status!r}")
    if not exp.decision_rule.strip():
        problems.append("decision_rule must state, in advance, how success/failure will be decided")
    return problems


def create_experiment(store: Store, ctx: BrandContext, registry: Registry, *,
                      business_objective: str, hypothesis: str, independent_variable: str,
                      control: str, treatment: str, target_platform: str, success_metric: str,
                      decision_rule: str, constant_conditions: list[str] | None = None,
                      target_account: str = "", sample_or_evaluation_threshold: str = "",
                      date_range_start: int | None = None, date_range_end: int | None = None,
                      risks: list[str] | None = None, approval_required: bool = True) -> Experiment:
    exp = Experiment(
        id=new_id(), brand=ctx.name, business_objective=business_objective, hypothesis=hypothesis,
        independent_variable=independent_variable, control=control, treatment=treatment,
        target_platform=target_platform, target_account=target_account, success_metric=success_metric,
        decision_rule=decision_rule, constant_conditions=constant_conditions or [],
        sample_or_evaluation_threshold=sample_or_evaluation_threshold,
        date_range_start=date_range_start, date_range_end=date_range_end, risks=risks or [],
        approval_required=approval_required, status="proposed", created_at=int(time.time()),
    )
    problems = validate_experiment(exp, registry, ctx)
    if problems:
        raise ExperimentValidationError("; ".join(problems))
    store.save_experiment(exp)
    return exp


def approve_experiment(store: Store, exp: Experiment, approver: str) -> Experiment:
    if not approver.strip():
        raise ExperimentValidationError("approve_experiment requires a non-empty approver identity")
    if exp.status != "proposed":
        raise ExperimentValidationError(f"experiment {exp.id} is {exp.status!r}, not 'proposed'")
    exp.status = "approved"
    exp.approved_by = approver
    store.save_experiment(exp)
    return exp


def evaluate_experiment(store: Store, exp: Experiment, outcome: dict, *, decided_by: str = "system") -> Experiment:
    """Records an outcome and moves status to 'evaluated' — this function
    interprets nothing; `outcome` must already state whatever conclusion
    was reached under the experiment's own pre-stated decision_rule."""
    if exp.status not in ("approved", "running"):
        raise ExperimentValidationError(f"experiment {exp.id} is {exp.status!r}, not 'approved'/'running'")
    exp.status = "evaluated"
    exp.outcome = {**outcome, "decided_by": decided_by, "decided_at": int(time.time())}
    store.save_experiment(exp)
    return exp
