"""Can this cell actually publish, right now? Asked BEFORE anything is planned.

WHY THIS EXISTS. A directive was filed naming a platform, a deadline and a
success target, asserting that publishing was impossible — and it was wrong,
because it read a module docstring instead of asking the module. The same
day, the opposite error: plans written assuming an upload would work, when
the session running them had no permission to perform one.

Both are the same defect. Capability was inferred from prose rather than
measured, so a plan could be written against a capability nobody checked.

WHAT THIS ANSWERS. Four preconditions, each independently observable:

  sink        an authorized sink exists and is constructible for the cell
  registry    that exact brand/platform row is live-enabled
  credential  credential material is present and structurally valid
  transport   a real transport exists (not a mock, not absent)

And a fifth that no code can observe about itself:

  permission  whether the RUNTIME may perform an outward action at all.
              A session's permission posture is not visible from inside the
              process. It is recorded as UNKNOWN and must be supplied by the
              caller from an actual attempt, never assumed. Treating it as
              satisfied because the other four passed is the exact mistake
              this module exists to prevent.

WHAT IT DOES NOT DO. No upload, no token refresh, no network call, no
browser. Constructing a credential handle reads a file path and validates
structure; it does not exchange anything. This module is safe to call from
a planning context, which is the entire point.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config.registry import Registry
from ..core.brand import BrandContext

# The answer for a precondition nothing in-process can observe.
UNKNOWN = "unknown"
SATISFIED = "satisfied"
UNSATISFIED = "unsatisfied"

PRECONDITIONS = ("sink", "registry", "credential", "transport", "permission")


@dataclass
class CapabilityReport:
    """A measured answer with its evidence, not an opinion.

    `publishable` is deliberately conservative: it is True only when every
    precondition is SATISFIED. An UNKNOWN is never rounded up — the whole
    failure mode being prevented is optimism about an unchecked capability.
    """

    brand: str
    platform: str
    preconditions: dict = field(default_factory=dict)
    checked_at: int = 0

    @property
    def publishable(self) -> bool:
        return all(p.get("status") == SATISFIED for p in self.preconditions.values())

    @property
    def blockers(self) -> list[str]:
        return [f"{name}: {p.get('detail', '')}"
                for name, p in self.preconditions.items()
                if p.get("status") != SATISFIED]

    def assert_publishable(self) -> None:
        if not self.publishable:
            raise CapabilityUnavailable(self.brand, self.platform, self.blockers)

    def summary(self) -> dict:
        return {"brand": self.brand, "platform": self.platform,
                "publishable": self.publishable,
                "preconditions": {k: v.get("status") for k, v in self.preconditions.items()},
                "blockers": self.blockers, "checked_at": self.checked_at}


class CapabilityUnavailable(Exception):
    def __init__(self, brand: str, platform: str, blockers: list[str]):
        self.brand, self.platform, self.blockers = brand, platform, blockers
        super().__init__(
            f"{brand}/{platform} cannot publish — unmet preconditions: " + "; ".join(blockers)
        )


def _entry(status: str, detail: str, **extra) -> dict:
    return {"status": status, "detail": detail, **extra}


def youtube_capability(ctx: BrandContext, registry: Registry, *,
                       sink_factory=None,
                       permission_probe=None,
                       clock=time.time) -> CapabilityReport:
    """Measure the five preconditions for one cell.

    `sink_factory` builds the sink to interrogate — injected so this module
    imports no transport and holds no credential path of its own. When
    omitted, the sink/credential/transport preconditions are reported
    UNKNOWN rather than guessed.

    `permission_probe` is a zero-argument callable returning True/False,
    supplied by a runtime that has actually attempted an outward action.
    Without one, `permission` is UNKNOWN — which makes the report
    non-publishable. That is intended: an unverified runtime capability is
    not a satisfied precondition.
    """
    platform = "youtube"
    out: dict[str, dict] = {}

    row = registry.account_row(ctx.name, platform) or {}
    channel_id = (row.get("channel_id") or "").strip()
    live = bool(registry.is_live_enabled(ctx.name, platform))
    if live and channel_id:
        out["registry"] = _entry(SATISFIED,
                                 f"row is live-enabled with a verified channel identity",
                                 live_status=row.get("live_status"))
    else:
        out["registry"] = _entry(
            UNSATISFIED,
            f"live_status={row.get('live_status')!r}, channel_id={'set' if channel_id else 'absent'}"
            " — only the registry row authorizes a live action",
            live_status=row.get("live_status"))

    if sink_factory is None:
        unknown = _entry(UNKNOWN, "no sink_factory supplied — not inferred from source text")
        out["sink"] = dict(unknown)
        out["credential"] = dict(unknown)
        out["transport"] = dict(unknown)
    else:
        try:
            sink = sink_factory()
        except Exception as e:  # noqa: BLE001 — any construction failure is a real blocker
            detail = f"sink could not be constructed: {type(e).__name__}: {e}"
            out["sink"] = _entry(UNSATISFIED, detail)
            out["credential"] = _entry(UNKNOWN, "not reached — sink construction failed")
            out["transport"] = _entry(UNKNOWN, "not reached — sink construction failed")
            out["permission"] = _entry(UNKNOWN, "not probed")
            return CapabilityReport(brand=ctx.name, platform=platform,
                                    preconditions=out, checked_at=int(clock()))

        # Ask the sink, never a docstring. refusal_reasons() is a pure check.
        reasons = list(sink.refusal_reasons())
        cred = [r for r in reasons if "credential" in r.lower()]
        trans = [r for r in reasons if "transport" in r.lower()]
        other = [r for r in reasons if r not in cred and r not in trans]

        out["credential"] = (_entry(UNSATISFIED, cred[0]) if cred
                             else _entry(SATISFIED, "credential material present and valid"))
        out["transport"] = (_entry(UNSATISFIED, trans[0]) if trans
                            else _entry(SATISFIED, "a real transport is attached"))
        # Refusals about enablement are the caller's to clear at execution
        # time and are not a statement about the cell's capability, so they
        # are reported rather than folded into a pass/fail here.
        out["sink"] = _entry(SATISFIED, "sink constructed and interrogated",
                             other_refusals=other)

    if permission_probe is None:
        out["permission"] = _entry(
            UNKNOWN,
            "runtime permission to perform an outward action has not been probed. A process "
            "cannot observe its own permission posture; supply permission_probe from an "
            "actual attempt. UNKNOWN is never treated as satisfied.")
    else:
        try:
            allowed = bool(permission_probe())
        except Exception as e:  # noqa: BLE001 — a probe that raises is a denial
            out["permission"] = _entry(UNSATISFIED, f"probe raised {type(e).__name__}: {e}")
        else:
            out["permission"] = (_entry(SATISFIED, "an outward action was permitted") if allowed
                                 else _entry(UNSATISFIED, "the runtime refused an outward action"))

    return CapabilityReport(brand=ctx.name, platform=platform,
                            preconditions=out, checked_at=int(clock()))
