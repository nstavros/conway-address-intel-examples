"""Reconcile the shared ledger against the live platform. Cron entry point.

    python3 publish_reconcile.py [--brand empires]

Runs BEFORE the daily review each morning so the review reads a ledger that
matches reality rather than one that matches what this system happens to have
done itself. On 2026-08-26 the gap between those two was fourteen posts, and a
session told the owner a video live for twenty hours had never been posted.

Reconciliation is a pure read of the public feed plus an idempotent write of
anything it finds. It publishes nothing, approves nothing, and makes no
outward-facing change. Re-running it is always safe.

YouTube only, deliberately: it is the one channel with a public feed. TikTok
and Instagram cannot self-heal and must be recorded at post time via
`python3 -m engage.publish.record`.
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
from engage.publish.reconcile import reconcile_youtube   # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--brand", default="empires")
    args = p.parse_args(argv)

    ctx = load_brand(ROOT / "brands", args.brand)
    store = Store(ctx.db_path())
    registry = load_registry(None)

    row = registry.account_row(ctx.name, "youtube") or {}
    channel = row.get("channel_id", "")
    if not channel:
        # Fail loudly. A reconciler that silently does nothing is worse than
        # one that is absent, because the ledger still looks maintained.
        print(f"{args.brand}: no verified youtube channel_id in the registry — "
              "cannot reconcile", file=sys.stderr)
        return 2

    src = YouTubeRSSSource(args.brand, channel_id=channel)
    report = reconcile_youtube(store, ctx, registry, rss_reader=src.poll)
    s = report.summary()

    print(f"{args.brand}/youtube: {s['observed']} observed · "
          f"{s['attributed']} attributable · {s['drift']} unaccounted · "
          f"{s['newly_recorded']} newly recorded")

    for post in report.unattributed_new:
        print(f"  + {post.get('platform_post_id')}  {post.get('title', '')[:70]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
