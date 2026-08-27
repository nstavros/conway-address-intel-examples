"""Show the Publishing Operations work queue: every open directive, assessed.

    python3 publish_queue.py                 # assess only, changes nothing
    python3 publish_queue.py --reconcile     # refresh from the live feed first

This is the thing that did not exist on 2026-08-26, when a directive was
filed at 08:52 with a 09:30 deadline and nothing ever read it.

Assessment is a pure read. It performs no upload, mints no token, and cannot
approve anything — a record not already owner-approved comes back as
needs_owner_approval, which is a report, not a step.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engage.config.loader import load_brand              # noqa: E402
from engage.config.registry import load_registry         # noqa: E402
from engage.core.store import Store                      # noqa: E402
from engage.ingest.youtube_rss import YouTubeRSSSource   # noqa: E402
from engage.publish import agent as pub_agent            # noqa: E402
from engage.publish.capability import youtube_capability  # noqa: E402
from engage.publish.reconcile import reconcile_youtube   # noqa: E402
from engage.publish.youtube_sink import (                # noqa: E402
    ConstraintSource,
    YouTubeUploadSink,
)

LABEL = {
    pub_agent.READY: "READY",
    pub_agent.NEEDS_OWNER_APPROVAL: "NEEDS OWNER APPROVAL",
    pub_agent.BLOCKED_DUPLICATE: "BLOCKED — ALREADY LIVE",
    pub_agent.BLOCKED_CAPABILITY: "BLOCKED — CAPABILITY",
    pub_agent.NO_RECORD: "NO CONTENT RECORD",
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--brand", default="empires")
    p.add_argument("--reconcile", action="store_true",
                   help="read the live public feed and record any drift first")
    args = p.parse_args(argv)

    ctx = load_brand(ROOT / "brands", args.brand)
    store = Store(ctx.db_path())
    registry = load_registry(None)
    channel = (registry.account_row(ctx.name, "youtube") or {}).get("channel_id", "")

    if args.reconcile:
        if not channel:
            print("no verified channel_id — cannot reconcile", file=sys.stderr)
            return 2
        src = YouTubeRSSSource(args.brand, channel_id=channel)
        rep = reconcile_youtube(store, ctx, registry, rss_reader=src.poll)
        s = rep.summary()
        print(f"reconciled: {s['observed']} observed · {s['attributed']} ours · "
              f"{s['drift']} unaccounted ({s['newly_recorded']} new)")
        print()

    def sink_factory():
        return YouTubeUploadSink(registry, ctx, enabled=True, store=store,
                                 constraint_source=ConstraintSource.from_file())

    # Permission is left UNPROBED on purpose. A process cannot observe its own
    # permission posture, and guessing it is the failure this whole preflight
    # exists to prevent. It shows as UNKNOWN until a real attempt says otherwise.
    cap = youtube_capability(ctx, registry, sink_factory=sink_factory)
    print(f"CAPABILITY {ctx.name}/youtube — publishable: {cap.publishable}")
    for name, entry in cap.preconditions.items():
        print(f"  {name:<11} {entry['status']:<11} {entry['detail'][:88]}")
    print()

    queue = pub_agent.work_queue(store, ctx, registry, sink_factory=sink_factory)
    if not queue:
        print("no open directives addressed to publishing operations.")
        return 0

    print(f"WORK QUEUE — {len(queue)} open directive(s)")
    for d in queue:
        print(f"\n  [{LABEL.get(d.status, d.status)}]  {d.directive_id}")
        print(f"    {d.deliverable}   (deadline: {d.deadline or 'none'})")
        if d.content_record_id:
            print(f"    record: {d.content_record_id}")
        for r in d.reasons:
            print(f"    - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
