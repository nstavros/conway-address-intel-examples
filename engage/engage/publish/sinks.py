"""Publish-sink interface for Phase 2 (Publishing Operations). Distinct
from publish/publisher.py's DryRunSink — that one is the pre-existing,
untouched dry-run sink for the OLD Draft-only publish() flow. This module's
sinks operate on the richer ContentRecord lifecycle (scheduled -> publishing
-> published -> verified) that operations.py drives.

Every sink implements the same three-method shape: schedule(), publish(),
verify() -> SinkResult. Only two sinks do anything at all in Phase 2:
DryRunSink (full simulation, clearly labeled) and ManualReviewSink (a
checklist, nothing more). Every other sink — the ones a real platform
integration will eventually need — is a stub that raises SinkUnavailableError
on every call. No stub silently no-ops or silently succeeds; failing loudly
is the point, since a silently-succeeding stub is indistinguishable from a
real integration to anything reading its result."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Sink result status vocabulary. "ambiguous" exists specifically so a
# caller can never mistake "I don't know what happened" for "safe to
# retry" — see publish/operations.py's retry classification.
SINK_STATUSES = ("ok", "transient_failure", "permanent_failure", "ambiguous")


class SinkUnavailableError(Exception):
    """Raised by every stub sink on every call — fails closed, never a
    silent no-op, never a silent success."""


@dataclass
class SinkResult:
    status: str  # one of SINK_STATUSES
    simulated: bool = True
    sink_job_id: str = ""
    published_url: str = ""      # never fabricated — "" unless a REAL sink set it
    platform_post_id: str = ""   # never fabricated — "" unless a REAL sink set it
    verification_status: str = ""
    verification_evidence: dict = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in SINK_STATUSES:
            raise ValueError(f"unknown sink status {self.status!r} (expected one of {SINK_STATUSES})")
        if not self.simulated and (self.published_url or self.platform_post_id):
            raise ValueError(
                "a non-simulated result claims a published_url/platform_post_id — "
                "no sink in this codebase is authorized to do that yet"
            )


class PublishSink:
    """Base interface. name is used in audit events and CLI output."""
    name = "base"

    def schedule(self, record, scheduled_at: int, timezone: str) -> SinkResult:
        raise NotImplementedError

    def publish(self, record) -> SinkResult:
        raise NotImplementedError

    def verify(self, record) -> SinkResult:
        raise NotImplementedError


class DryRunSink(PublishSink):
    """Simulates scheduling, publishing, and verification end to end.
    Never makes a network call, never opens a browser, never touches
    credentials — there simply is no code path here that could. Every
    result is tagged simulated=True and every URL/post-id field is left
    empty, so nothing downstream can mistake this for a real publish.

    `fault_injector` is an optional callable returning one of SINK_STATUSES,
    used ONLY by tests to deterministically exercise every failure path —
    it defaults to always "ok" and is never wired to anything random or
    time-based, matching this codebase's offline-testable convention."""

    name = "dry_run"

    def __init__(self, fault_injector: Callable[[], str] | None = None):
        self._fault_injector = fault_injector or (lambda: "ok")

    def _outcome(self) -> str:
        status = self._fault_injector()
        if status not in SINK_STATUSES:
            raise ValueError(f"fault_injector returned {status!r}, not one of {SINK_STATUSES}")
        return status

    def schedule(self, record, scheduled_at: int, timezone: str) -> SinkResult:
        status = self._outcome()
        if status != "ok":
            return SinkResult(status=status, sink_job_id="", error=f"simulated {status} on schedule")
        return SinkResult(status="ok", sink_job_id=f"dryrun-sched-{record.id}",
                          verification_evidence={"simulated": True, "step": "schedule"})

    def publish(self, record) -> SinkResult:
        status = self._outcome()
        if status != "ok":
            return SinkResult(status=status, error=f"simulated {status} on publish")
        return SinkResult(status="ok", sink_job_id=f"dryrun-pub-{record.id}",
                          verification_evidence={"simulated": True, "step": "publish"})

    def verify(self, record) -> SinkResult:
        status = self._outcome()
        if status != "ok":
            return SinkResult(status=status, error=f"simulated {status} on verify")
        return SinkResult(status="ok", verification_status="simulated_verified",
                          verification_evidence={"simulated": True, "step": "verify",
                                                 "note": "no real platform was contacted"})


class ManualReviewSink(PublishSink):
    """Produces a precise, non-actionable checklist and stops — it never
    drives a record to 'published'/'verified' itself, because nothing
    automated actually posted anything. A human does the real posting
    outside this system; this sink's only job is to hand them exactly what
    they need, and to record that the item is ready. No browser, no
    network call, no credentials, anywhere in this class."""

    name = "manual_review"

    def schedule(self, record, scheduled_at: int, timezone: str) -> SinkResult:
        return SinkResult(status="ok", sink_job_id=f"manual-{record.id}")

    def publish(self, record) -> SinkResult:
        brief = record.brief or {}
        checklist = {
            "brand": record.brand,
            "platform": record.platform,
            "caption": brief.get("caption", ""),
            "hook": brief.get("hook", ""),
            "cta": brief.get("cta", ""),
            "format": brief.get("format", ""),
            "scheduled_at": record.scheduled_at,
            "timezone": record.timezone,
            "instructions": (
                "Post this exact caption text, unedited, to the platform above "
                "at the scheduled time. This checklist is informational only — "
                "no part of this system will post it for you."
            ),
        }
        return SinkResult(status="ok", sink_job_id=f"manual-{record.id}",
                          verification_status="ready_for_manual_posting",
                          verification_evidence={"simulated": True, "checklist": checklist})

    def verify(self, record) -> SinkResult:
        # Manual review never claims verification — only a human confirming
        # a real post could justify that, and this system does not offer a
        # confirm-it-happened path in Phase 2.
        raise SinkUnavailableError(
            "manual_review has no verify step — verification of a manually "
            "posted item is out of scope for Phase 2"
        )


def _stub(name: str) -> type[PublishSink]:
    def _raise(self, *a, **k):
        raise SinkUnavailableError(
            f"{name} sink is not implemented — disabled by design until an "
            "authorized, tested integration replaces this stub"
        )
    return type(f"{name.title().replace('_', '')}Sink", (PublishSink,),
               {"name": name, "schedule": _raise, "publish": _raise, "verify": _raise})


ApiSink = _stub("api")
MCPConnectorSink = _stub("mcp_connector")
SchedulerSink = _stub("scheduler")
BrowserSink = _stub("browser")

SINKS: dict[str, type[PublishSink]] = {
    "dry_run": DryRunSink,
    "manual_review": ManualReviewSink,
    "api": ApiSink,
    "mcp": MCPConnectorSink,
    "scheduler": SchedulerSink,
    "browser": BrowserSink,
}
