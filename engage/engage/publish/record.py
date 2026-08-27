"""Record a post that was made outside this system. One command, five seconds.

    python3 -m engage.publish.record --brand <brand> --platform tiktok \\
        --post-id 7412... --title "The helmet everyone draws..."

WHY THIS EXISTS. YouTube publishes a public feed, so a post made by hand there
is recoverable — reconcile.py finds it. TikTok and Instagram publish nothing of
the kind. A post made there by a person or another tool is invisible to this
system permanently unless somebody writes it down, and "invisible" here means
the duplicate guard cannot see it either.

On 2026-08-26 the store believed one video was live while fourteen were. The
YouTube half of that healed itself on first reconciliation. The TikTok half
cannot, and never will, without this.

Deliberately tiny and dependency-free at the call site: the friction of
recording has to be lower than the friction of skipping it, or it gets skipped.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config.loader import load_brand
from ..core.store import Store

# Platforms whose state this system can recover on its own. Anything not
# listed here is recoverable ONLY from a record made at the time of posting.
SELF_HEALING = ("youtube",)


def _brands_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "brands"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="engage.publish.record",
        description="Record a post made outside ENGAGE so the system stops "
                    "believing the slot is still open.")
    p.add_argument("--brand", required=True)
    p.add_argument("--platform", required=True,
                   help="youtube | tiktok | instagram")
    p.add_argument("--post-id", required=True,
                   help="the platform's own id for the post — never a title or url")
    p.add_argument("--title", default="",
                   help="the title/caption as actually published, verbatim")
    p.add_argument("--url", default="")
    p.add_argument("--published-at", type=int, default=None,
                   help="unix seconds; omit if unknown")
    p.add_argument("--brands-dir", default=None)
    args = p.parse_args(argv)

    brands = Path(args.brands_dir) if args.brands_dir else _brands_dir()
    try:
        ctx = load_brand(brands, args.brand)
    except Exception as e:  # noqa: BLE001 — surface the real cause to a human
        print(f"cannot load brand {args.brand!r}: {e}", file=sys.stderr)
        return 2

    store = Store(ctx.db_path())
    existing = store.get_external_post(args.brand, args.platform, args.post_id)

    store.record_external_post(
        brand=args.brand, platform=args.platform, platform_post_id=args.post_id,
        title=args.title, url=args.url, published_at=args.published_at,
        source="manual_record",
    )

    verb = "updated" if existing else "recorded"
    print(f"{verb}: {args.brand}/{args.platform} {args.post_id}"
          + (f"  {args.title[:60]!r}" if args.title else ""))

    if not args.title:
        print("  WARNING: no --title given. The duplicate guard matches on title, "
              "so this row will not stop a re-post of the same video.",
              file=sys.stderr)
    if args.platform not in SELF_HEALING:
        print(f"  ({args.platform} has no public feed — this record is the only "
              "way this system will ever know about that post.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
