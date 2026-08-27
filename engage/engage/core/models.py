"""Data models shared across pipeline stages."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Post:
    """A post seen by the listening layer, or one of our own published posts."""

    id: str
    brand: str
    platform: str
    author: str
    text: str
    url: str = ""
    created_at: int = 0  # unix seconds
    # engagement snapshots: (unix_ts, total_engagement) — two or more enable momentum
    snapshots: list[tuple[int, int]] = field(default_factory=list)
    own: bool = False  # True when the post is ours (triage/measure, never an opportunity)


@dataclass
class Opportunity:
    post_id: str
    brand: str
    score: float
    components: dict[str, float]
    excluded: bool = False
    exclusion_reason: str = ""


# Draft lifecycle:
#   DRAFT -> (gates) -> PENDING | BLOCKED
#   PENDING -> (human) -> APPROVED -> (publisher) -> PUBLISHED
#   any edit resets to DRAFT and invalidates approval.
DRAFT_STATUSES = ("DRAFT", "PENDING", "BLOCKED", "APPROVED", "PUBLISHED")


@dataclass
class Draft:
    id: str
    brand: str
    kind: str  # reply | original | repurpose
    platform: str
    text: str
    post_id: str = ""  # source post for replies
    material_id: str = ""  # source material for originals/repurposes
    funnel_class: str = ""  # TOP | MIDDLE | BOTTOM ("" when brand has no funnel)
    status: str = "DRAFT"
    gate_reasons: list[str] = field(default_factory=list)
    angle: str = ""

    @property
    def hash(self) -> str:
        return content_hash(self.text)


@dataclass
class SourceMaterial:
    id: str
    brand: str
    title: str
    text: str
    kind: str = "note"  # note | script | deal_update | photo_caption | article


# ContentRecord lifecycle (Phase 1 — see drafting/originals.py):
#   draft -> review_required -> [approved] -> scheduled -> publishing -> published -> verified
#   draft/review_required -> rejected | cancelled (terminal)
#   any non-terminal -> paused -> (explicit resume target)
#   publishing/published -> failed -> review_required | cancelled
# "approved" is reachable ONLY through originals.approve_content_record() —
# it requires a human approval record and is never a target of the generic
# transition() dispatcher. Phase 1 code only ever produces draft,
# review_required, rejected, and cancelled; scheduled/publishing/published/
# verified/paused/failed are defined now so Phase 2 (Publishing Operations)
# extends this model rather than replacing it.
LIFECYCLE_STATES = (
    "draft", "review_required", "approved", "scheduled", "publishing",
    "published", "verified", "paused", "failed", "rejected", "cancelled",
)

LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft":           frozenset({"review_required", "rejected", "cancelled"}),
    "review_required": frozenset({"rejected", "cancelled", "paused"}),  # "approved" excluded on purpose
    "approved":        frozenset({"scheduled", "cancelled", "paused"}),
    "scheduled":       frozenset({"publishing", "cancelled", "paused"}),
    "publishing":      frozenset({"published", "failed"}),
    "published":       frozenset({"verified", "failed"}),
    "verified":        frozenset(),
    "paused":          frozenset({"review_required", "scheduled", "cancelled"}),
    # "scheduled" added for Phase 2 (Publishing Operations): a classified
    # transient dry-run failure may be requeued for retry, within a
    # configured limit — see publish/operations.py retry_publish(). A
    # permanent or ambiguous failure is never auto-retried; only an
    # explicit, separate call can requeue it, and only while classified
    # transient and under the retry limit.
    "failed":          frozenset({"review_required", "cancelled", "scheduled"}),
    "rejected":        frozenset(),
    "cancelled":       frozenset(),
}


class LifecycleError(Exception):
    pass


@dataclass
class ContentRecord:
    """The structured content record spanning brief -> draft -> (eventually)
    published + verified. Wraps a Draft (kind='original' or 'repurpose') with
    the richer lifecycle vocabulary; the underlying Draft.status keeps
    governing the existing approval/gates/publish machinery unchanged."""

    id: str
    brand: str
    platform: str
    kind: str  # original | repurpose
    draft_id: str = ""
    brief: dict = field(default_factory=dict)
    pillar: str = ""
    lifecycle_status: str = "draft"

    # Phase 2 (Publishing Operations) — scheduling and verification.
    # published_url / platform_post_id stay "" for every dry_run and
    # manual_review outcome: they are populated ONLY by a real sink (none
    # exist yet), never fabricated. verification_evidence is a dict, always
    # tagged {"simulated": True} by DryRunSink/ManualReviewSink so nothing
    # downstream can mistake a simulation for a real, verified publish.
    scheduled_at: int | None = None  # unix seconds
    timezone: str = "America/New_York"
    publish_method: str = ""  # "" | dry_run | manual_review | api | mcp | scheduler | browser
    sink_job_id: str = ""
    published_at: int | None = None
    verification_status: str = ""
    verification_evidence: dict = field(default_factory=dict)
    published_url: str = ""
    platform_post_id: str = ""
    retry_count: int = 0
    last_error: str = ""


# Phase 3 (Performance Intelligence) — every metric field is optional and
# defaults to None, never 0 or a fabricated value: "no data" and "zero"
# are different facts and this model never conflates them. data_coverage
# and attribution_status exist specifically so missing data is legible as
# missing, not silently read as poor performance.
PERFORMANCE_METRIC_FIELDS = (
    "impressions", "reach", "non_follower_reach", "views", "watch_time",
    "completion_rate", "likes", "comments", "shares", "saves",
    "profile_visits", "link_clicks", "inbound_dms", "qualified_leads",
    "booked_calls", "pipeline_value", "revenue_attributed", "sentiment",
    "response_time",
)

DATA_COVERAGE_VALUES = ("none", "partial", "complete")
ATTRIBUTION_STATUS_VALUES = ("unattributed", "estimated", "attributed", "not_applicable")
PERFORMANCE_SOURCES = ("manual_import", "internal_event", "api", "mcp")


@dataclass
class PerformanceRecord:
    """One measurement row, keyed to the exact content record and the
    approved-content hash it measures — never to a bare post_id, so a
    performance row can never silently drift onto the wrong (or a later
    edited) version of the content."""

    id: str
    brand: str
    content_record_id: str
    content_hash: str
    account: str = ""
    platform: str = ""
    campaign: str | None = None
    pillar: str = ""
    format: str = ""
    hook_type: str = ""
    cta: str = ""
    creative_asset_id: str | None = None
    publish_time: int | None = None
    metrics: dict = field(default_factory=dict)  # subset of PERFORMANCE_METRIC_FIELDS -> float
    data_coverage: str = "none"       # one of DATA_COVERAGE_VALUES
    attribution_status: str = "unattributed"  # one of ATTRIBUTION_STATUS_VALUES
    source: str = "manual_import"     # one of PERFORMANCE_SOURCES
    data_collected_at: int | None = None


# Phase 3 — experiment ledger. A DIFFERENT state machine from
# LIFECYCLE_STATES above (a different entity — experiments are advisory
# records that never touch a calendar, draft, schedule, publication, or
# account; reusing content-record lifecycle vocabulary for them would
# blur two genuinely different concepts, not reduce duplication).
EXPERIMENT_STATUSES = ("proposed", "approved", "running", "evaluated", "rejected", "cancelled")


@dataclass
class Experiment:
    id: str
    brand: str
    business_objective: str
    hypothesis: str
    independent_variable: str
    control: str
    treatment: str
    target_platform: str
    success_metric: str
    decision_rule: str
    constant_conditions: list[str] = field(default_factory=list)
    target_account: str = ""
    sample_or_evaluation_threshold: str = ""
    date_range_start: int | None = None
    date_range_end: int | None = None
    risks: list[str] = field(default_factory=list)
    approval_required: bool = True
    approved_by: str = ""
    status: str = "proposed"
    outcome: dict = field(default_factory=dict)
    created_at: int | None = None


# Phase 3 — every finding and recommendation carries exactly one of these.
# "validated_learning" requires evidence meeting a configured minimum
# threshold (see measure/kpi.py MIN_EVIDENCE_THRESHOLD) — nothing is
# promoted to it just because it looks consistent on a small sample.
FINDING_LABELS = ("observation", "hypothesis", "early_signal", "validated_learning", "insufficient_data")

# Recommendation targets. "publishing" and "engagement" already exist as
# drafting.originals.HANDOFF_TARGETS (single-content-record handoffs);
# "drafting" and "ceo" are added there too so a recommendation about ONE
# specific content record can reuse write_handoff() unchanged. Pattern-level
# recommendations (not tied to one record) use measure/intelligence.py's
# write_recommendation() instead — a sibling mechanism, not a duplicate:
# write_handoff() has no way to represent "no single record" and forcing
# that shape onto it would be the more invasive change.
# "performance" added in Phase 5 (engagement/agent.py's
# emit_engagement_insights): Phase 3/4 only ever needed Performance
# Intelligence to WRITE recommendations, never to RECEIVE one, so the
# target was never added. Engagement is the first caller that needs to send
# a recommendation TO Performance Intelligence — a genuine gap, not a
# design choice, closed here.
RECOMMENDATION_TARGETS = ("drafting", "publishing", "engagement", "ceo", "performance")


# Phase 5 (Engagement) — extends triage/comments.py's 3-way
# HUMAN/DRAFTABLE/SKIP into the fuller vocabulary this phase needs, without
# changing what that module returns (engagement/triage_ext.py wraps it).
ENGAGEMENT_TRIAGE_CLASSES = (
    "human_needed", "draftable", "skip", "spam_or_low_value",
    "possible_lead", "reputation_or_safety_risk", "insufficient_context",
)


@dataclass
class Comment:
    """One imported comment, tied to an exact brand/platform/account and,
    where the source is our own published content, the exact
    publication/version hash — so a comment can never be silently
    reattributed to a different or later-edited post."""

    id: str
    brand: str
    platform: str
    account: str
    author: str
    text: str
    source_type: str  # "content_record" | "post"
    source_id: str
    external_id: str = ""       # the platform's own comment id, when known
    content_hash: str = ""      # the source's approved-content hash, when source_type == content_record
    text_hash: str = ""         # sha256 of `text` — set by the store layer
    imported_at: int | None = None
    triage_class: str = ""
    triage_confidence: float = 0.0
    triage_reason: str = ""


# A DIFFERENT state machine from LIFECYCLE_STATES — a reply draft never
# enters the ContentRecord/Publishing Operations pipeline at all (no
# scheduled/publishing/published/verified states exist for it), because
# replying is manual-only in this phase and indefinitely: "approved" here
# means "approved for manual posting", nothing else, and nothing in this
# codebase ever automates that step. Reusing ContentRecord's vocabulary
# would imply a machine-postable path that does not and must not exist.
REPLY_REVIEW_STATUSES = ("draft", "review_required", "approved_for_manual_posting",
                         "rejected", "expired", "paused")

REPLY_REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft":                       frozenset({"review_required", "rejected"}),
    "review_required":             frozenset({"approved_for_manual_posting", "rejected", "paused", "expired"}),
    "approved_for_manual_posting": frozenset({"expired"}),  # posting happens OUTSIDE this system, manually
    "rejected":                    frozenset(),
    "expired":                     frozenset(),
    "paused":                      frozenset({"review_required", "rejected"}),
}


@dataclass
class ReplyDraft:
    """A suggested reply — bound to the exact comment text and its own
    generated text by hash, so any edit to either invalidates the binding
    and forces a fresh review, the same discipline Draft/ContentRecord
    already apply to original content."""

    id: str
    brand: str
    platform: str
    account: str
    comment_id: str
    comment_text_hash: str
    draft_text: str
    draft_text_hash: str = ""
    angle: str = ""
    brand_strategy_source: str = ""  # title of the planning source at generation time
    review_status: str = "draft"
    gate_reasons: list[str] = field(default_factory=list)
    generated_at: int | None = None
