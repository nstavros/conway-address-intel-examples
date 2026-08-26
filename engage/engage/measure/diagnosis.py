"""Analysis and diagnosis over the performance ledger. Every finding
carries exactly one label from core.models.FINDING_LABELS, a confidence
tier, and its data limitations — never a bare claim. Nothing here declares
a "winner" on awareness metrics (views/likes/reach) alone: repeated_winners
and repeated_underperformers always require an intent-or-business metric to
also clear its baseline, and always require more than one post."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.models import FINDING_LABELS
from . import kpi

DIMENSIONS = ("platform", "account", "campaign", "pillar", "format", "hook_type", "cta", "creative_asset_id")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def posting_window(publish_time: int | None) -> str:
    """Coarse, honestly-scoped posting-window bucket: day of week only
    (UTC, deterministic — no local-timezone dependency). Not a claim about
    optimal posting HOUR, which this system has no evidence for."""
    if publish_time is None:
        return "(unknown)"
    return _WEEKDAYS[time.gmtime(publish_time).tm_wday]


def _metric_value(record, metric: str) -> float | None:
    if metric in kpi.NORMALIZED_METRICS:
        return kpi.NORMALIZED_METRICS[metric](record.metrics)
    return record.metrics.get(metric)


def _group_by(records: list, dimension: str) -> dict[str, list]:
    groups: dict[str, list] = {}
    for r in records:
        key = posting_window(r.publish_time) if dimension == "posting_window" else (getattr(r, dimension, "") or "")
        key = key or "(unspecified)"
        groups.setdefault(key, []).append(r)
    return groups


def segment(records: list, by: str) -> dict[str, list]:
    if by not in DIMENSIONS + ("posting_window",):
        raise ValueError(f"unknown segmentation dimension {by!r} (expected one of {DIMENSIONS + ('posting_window',)})")
    return _group_by(records, by)


def baseline_for(records: list, metric: str) -> float | None:
    """Median of `metric` across records that have it — the same
    median-as-baseline convention measure/collector.py already established
    for performance_norm, reused here rather than a new method. None (not
    0) when fewer than 2 data points exist — a median of one point isn't a
    baseline."""
    vals = sorted(v for v in (_metric_value(r, metric) for r in records) if v is not None)
    if len(vals) < 2:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def label_finding(n_records: int, consistent: bool, threshold: int = kpi.MIN_EVIDENCE_THRESHOLD) -> str:
    """The one rule every finding's label comes from:
      0 records            -> insufficient_data
      1 record              -> observation
      2..threshold-1, mixed -> hypothesis
      2..threshold-1, consistent -> early_signal
      >=threshold, mixed    -> hypothesis (still not clean enough to validate)
      >=threshold, consistent -> validated_learning
    """
    if n_records == 0:
        return "insufficient_data"
    if n_records == 1:
        return "observation"
    if n_records < threshold:
        return "early_signal" if consistent else "hypothesis"
    return "validated_learning" if consistent else "hypothesis"


@dataclass
class Finding:
    label: str
    kind: str
    dimension: str
    key: str
    summary: str
    confidence: str
    evidence: dict = field(default_factory=dict)
    data_limitations: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.label not in FINDING_LABELS:
            raise ValueError(f"unknown finding label {self.label!r}")


def _confidence_for(n: int, threshold: int) -> str:
    if n == 0:
        return "none"
    if n < threshold:
        return "low"
    return "high" if n >= threshold * 2 else "medium"


def _quadrant_findings(records: list, dimension: str, awareness_metric: str, intent_metric: str,
                       kind: str, want_awareness_high: bool, want_intent_high: bool,
                       min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    by_platform = _group_by(records, "platform")
    baselines = {
        plat: {awareness_metric: baseline_for(rs, awareness_metric), intent_metric: baseline_for(rs, intent_metric)}
        for plat, rs in by_platform.items()
    }
    findings = []
    for key, group in _group_by(records, dimension).items():
        matches = []
        for r in group:
            b = baselines.get(r.platform, {})
            a_val, i_val = _metric_value(r, awareness_metric), _metric_value(r, intent_metric)
            a_base, i_base = b.get(awareness_metric), b.get(intent_metric)
            if a_val is None or i_val is None or a_base is None or i_base is None:
                continue
            a_ok = (a_val >= a_base) == want_awareness_high
            i_ok = (i_val >= i_base) == want_intent_high
            if a_ok and i_ok:
                matches.append(r)
        n = len(matches)
        label = label_finding(n, consistent=True, threshold=min_occurrences)
        if n == 0:
            continue
        limitations = []
        if len(group) < min_occurrences:
            limitations.append(f"only {len(group)} post(s) in this {dimension} group")
        findings.append(Finding(
            label=label, kind=kind, dimension=dimension, key=key,
            summary=f"{dimension}={key!r}: {n}/{len(group)} posts show {kind} "
                    f"({awareness_metric} {'above' if want_awareness_high else 'below'} baseline, "
                    f"{intent_metric} {'above' if want_intent_high else 'below'} baseline)",
            confidence=_confidence_for(n, min_occurrences),
            evidence={"content_record_ids": [m.content_record_id for m in matches], "n": n},
            data_limitations=limitations,
        ))
    return findings


def repeated_winners(records: list, dimension: str, awareness_metric: str = "views",
                     intent_metric: str = "qualified_leads",
                     min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    """Never on awareness alone: a post only counts as a 'win' when it
    clears baseline on BOTH the awareness metric and an intent/business
    metric. A group needs more than one qualifying post before its label
    can reach validated_learning."""
    return _quadrant_findings(records, dimension, awareness_metric, intent_metric,
                              "repeated_winner", True, True, min_occurrences)


def repeated_underperformers(records: list, dimension: str, awareness_metric: str = "views",
                             intent_metric: str = "qualified_leads",
                             min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    return _quadrant_findings(records, dimension, awareness_metric, intent_metric,
                              "repeated_underperformer", False, False, min_occurrences)


def high_reach_low_intent(records: list, dimension: str = "pillar", awareness_metric: str = "reach",
                          intent_metric: str = "link_clicks",
                          min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    return _quadrant_findings(records, dimension, awareness_metric, intent_metric,
                              "high_reach_low_intent", True, False, min_occurrences)


def low_reach_high_intent(records: list, dimension: str = "pillar", awareness_metric: str = "reach",
                          intent_metric: str = "link_clicks",
                          min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    return _quadrant_findings(records, dimension, awareness_metric, intent_metric,
                              "low_reach_high_intent", False, True, min_occurrences)


def publishing_or_coverage_failures(performance_records: list, content_records: list) -> list[Finding]:
    """Reuses Phase 2's own ContentRecord.lifecycle_status (never
    duplicated) for operational failures, and this ledger's data_coverage
    field for measurement gaps. Missing data is reported as missing, never
    silently read as poor performance."""
    findings = []
    failed = [c for c in content_records if c.lifecycle_status == "failed"]
    if failed:
        findings.append(Finding(
            label=label_finding(len(failed), consistent=True), kind="publishing_failure",
            dimension="content_record", key="(multiple)",
            summary=f"{len(failed)} content record(s) in 'failed' lifecycle status",
            confidence=_confidence_for(len(failed), kpi.MIN_EVIDENCE_THRESHOLD),
            evidence={"content_record_ids": [c.id for c in failed], "n": len(failed)},
            data_limitations=[],
        ))
    no_coverage = [p for p in performance_records if p.data_coverage == "none"]
    if no_coverage:
        findings.append(Finding(
            label="insufficient_data", kind="coverage_gap", dimension="content_record", key="(multiple)",
            summary=f"{len(no_coverage)} published item(s) have no performance data collected yet",
            confidence="none",
            evidence={"content_record_ids": [p.content_record_id for p in no_coverage], "n": len(no_coverage)},
            data_limitations=["no metrics imported for these items — not evidence of poor performance"],
        ))
    return findings


def lead_quality_issues(records: list, min_occurrences: int = kpi.MIN_EVIDENCE_THRESHOLD) -> list[Finding]:
    """Funnel-drop-off signal: meaningful clicks but no qualified leads,
    repeated across posts — flagged as a possible lead-quality issue, never
    asserted as a fact from one post."""
    candidates = [r for r in records
                 if r.metrics.get("link_clicks") and r.metrics.get("qualified_leads") == 0]
    n = len(candidates)
    if n == 0:
        return []
    return [Finding(
        label=label_finding(n, consistent=True, threshold=min_occurrences), kind="lead_quality_issue",
        dimension="brand", key="(all)",
        summary=f"{n} post(s) generated link clicks but zero qualified leads",
        confidence=_confidence_for(n, min_occurrences),
        evidence={"content_record_ids": [c.content_record_id for c in candidates], "n": n},
        data_limitations=[] if n >= min_occurrences else [f"only {n} post(s) show this pattern so far"],
    )]
