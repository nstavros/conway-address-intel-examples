"""ENGAGE CLI. Every content command requires --brand; there is no
--all-brands write path. Publishing defaults to dry-run and v1 has no live
sink at all."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .approval.queue import ApprovalError, approve, edit_draft, submit
from .config import discover_brands, load_brand
from .core.models import Draft, SourceMaterial, new_id
from .core.store import Store
from .drafting.replies import draft_replies
from .funnel.classifier import FunnelViolation, check_week
from .ingest.chatplace import ChatplaceMediaSource
from .ingest.manual import ManualSource
from .publish.publisher import DryRunSink, publish
from .report.digest import build_digest
from .scoring.scorer import score_post


def _brands_dir(args) -> Path:
    return Path(args.brands_dir or os.environ.get("ENGAGE_BRANDS_DIR", "brands"))


def _load(args):
    ctx = load_brand(_brands_dir(args), args.brand)
    return ctx, Store(ctx.db_path())


def _stub_backend(prompt: str) -> str:
    raise SystemExit(
        "No LLM backend configured. Pass --backend-cmd '<command reading prompt "
        "on stdin, writing completion on stdout>' (e.g. an LLM CLI)."
    )


def _cmd_backend(cmd: str):
    import subprocess

    def backend(prompt: str) -> str:
        out = subprocess.run(cmd, shell=True, input=prompt, capture_output=True,
                             text=True, timeout=180)
        if out.returncode != 0:
            raise RuntimeError(f"backend failed: {out.stderr[:300]}")
        return out.stdout
    return backend


def cmd_brands(args):
    for name in discover_brands(_brands_dir(args)):
        ctx = load_brand(_brands_dir(args), name)
        print(f"{name}: platforms={ctx.config['platforms']} "
              f"priority={ctx.config.get('priority', 'normal')}")


def cmd_ingest(args):
    ctx, store = _load(args)
    if args.source == "manual":
        src = ManualSource(ctx.name, args.file)
    elif args.source == "chatplace":
        src = ChatplaceMediaSource(ctx.name, args.file)
    else:
        raise SystemExit(f"unknown source {args.source}")
    posts = src.poll()
    for p in posts:
        store.upsert_post(p)
    print(f"[{ctx.name}] ingested {len(posts)} posts from {args.file}")


def cmd_score(args):
    ctx, store = _load(args)
    posts = store.list_posts(ctx.name, own=False)
    scored = excluded = 0
    for p in posts:
        o = score_post(p, ctx, store)
        store.save_opportunity(o)
        scored += 1
        excluded += int(o.excluded)
    print(f"[{ctx.name}] scored {scored} posts ({excluded} excluded)")


def cmd_opportunities(args):
    ctx, store = _load(args)
    for o, p in store.top_opportunities(ctx.name, args.limit):
        print(f"[{o.score:.3f}] {p.author} ({p.platform}) {p.url}")
        print(f"    {p.text.splitlines()[0][:100]}")
        print(f"    components: {o.components}")


def cmd_material(args):
    ctx, store = _load(args)
    text = Path(args.file).read_text(encoding="utf-8")
    mid = args.id or new_id()
    store.add_material(mid, args.title or Path(args.file).stem, text, args.kind)
    print(f"[{ctx.name}] material {mid} added")


def cmd_draft_reply(args):
    ctx, store = _load(args)
    post = store.get_post(args.post_id, ctx.name)
    if post is None:
        raise SystemExit(f"no post {args.post_id} — ingest first")
    backend = _cmd_backend(args.backend_cmd) if args.backend_cmd else _stub_backend
    llm = backend if args.backend_cmd else None
    drafts = draft_replies(post, ctx, backend)
    for d in drafts:
        submit(store, ctx, d, llm=llm)
        print(f"[{ctx.name}] draft {d.id} ({d.angle}) -> {d.status}")
        if d.gate_reasons:
            print(f"    gates: {d.gate_reasons}")
        print(f"    {d.text[:200]}")
    store.record_touch(post.author, post.platform, post.id)


def cmd_submit(args):
    """Submit externally written text (e.g. from a drafting session) through
    gates into the approval queue — still the only write path."""
    ctx, store = _load(args)
    text = Path(args.file).read_text(encoding="utf-8")
    sources: tuple[str, ...] = ()
    if args.material_id:
        m = store.get_material(args.material_id)
        if m is None:
            raise SystemExit(f"no material {args.material_id}")
        sources = (m["text"],)
    llm = _cmd_backend(args.backend_cmd) if args.backend_cmd else None
    d = Draft(id=new_id(), brand=ctx.name, kind=args.kind, platform=args.platform,
              text=text, material_id=args.material_id or "")
    submit(store, ctx, d, sources, llm)
    print(f"[{ctx.name}] draft {d.id} -> {d.status}")
    for r in d.gate_reasons:
        print(f"    {r}")


def cmd_queue(args):
    ctx, store = _load(args)
    if args.action == "list":
        for status in ("PENDING", "BLOCKED", "APPROVED"):
            drafts = store.list_drafts(ctx.name, status=status)
            if drafts:
                print(f"{status}:")
                for d in drafts:
                    print(f"  {d.id} [{d.kind}/{d.platform}] {d.text.splitlines()[0][:80]}")
    elif args.action == "show":
        d = store.get_draft(args.draft_id, ctx.name)
        if not d:
            raise SystemExit("no such draft")
        print(f"{d.id} {d.status} [{d.kind}/{d.platform}] funnel={d.funnel_class or '-'}")
        print(d.text)
        if d.gate_reasons:
            print(f"gates: {d.gate_reasons}")
    elif args.action == "approve":
        try:
            d = approve(store, ctx, args.draft_id)
            print(f"approved {d.id} (hash {d.hash[:12]}…)")
        except ApprovalError as e:
            raise SystemExit(str(e))
    elif args.action == "edit":
        new_text = Path(args.file).read_text(encoding="utf-8")
        d = edit_draft(store, ctx, args.draft_id, new_text)
        print(f"edited {d.id} — approval revoked, status {d.status}; re-submit through gates")


def cmd_funnel_check(args):
    ctx, store = _load(args)
    week_ago = int(time.time()) - 7 * 86400
    drafts = [d for d in store.list_drafts(ctx.name)
              if d.status in ("PENDING", "APPROVED")]
    try:
        report = check_week(drafts, ctx)
        print(json.dumps(report, indent=2))
    except FunnelViolation as e:
        raise SystemExit(f"FUNNEL VIOLATION: {e}")


def cmd_publish(args):
    ctx, store = _load(args)
    dry = not args.live
    plan = publish(store, ctx, dry_run=dry, sink=DryRunSink() if not dry else None)
    if not plan:
        print(f"[{ctx.name}] nothing approved to publish")
    for item in plan:
        print(f"[{ctx.name}] {item.note}")
        print(f"    ({item.draft.platform}/{item.draft.kind}) {item.draft.text[:200]}")


def cmd_digest(args):
    names = [args.brand] if args.brand else discover_brands(_brands_dir(args))
    sections = []
    for name in names:
        ctx = load_brand(_brands_dir(args), name)
        sections.append((ctx, Store(ctx.db_path())))
    print(build_digest(sections))


def main(argv=None):
    p = argparse.ArgumentParser(prog="engage",
                                description="Multi-brand social engagement engine")
    p.add_argument("--brands-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def brand_cmd(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--brand", required=True)
        sp.set_defaults(fn=fn)
        return sp

    sub.add_parser("brands").set_defaults(fn=cmd_brands)

    sp = brand_cmd("ingest", cmd_ingest)
    sp.add_argument("--source", choices=["manual", "chatplace"], required=True)
    sp.add_argument("--file", required=True)

    brand_cmd("score", cmd_score)

    sp = brand_cmd("opportunities", cmd_opportunities)
    sp.add_argument("--limit", type=int, default=10)

    sp = brand_cmd("material", cmd_material)
    sp.add_argument("--file", required=True)
    sp.add_argument("--title", default="")
    sp.add_argument("--kind", default="note")
    sp.add_argument("--id", default="")

    sp = brand_cmd("draft-reply", cmd_draft_reply)
    sp.add_argument("--post-id", required=True)
    sp.add_argument("--backend-cmd", default="")

    sp = brand_cmd("submit", cmd_submit)
    sp.add_argument("--file", required=True)
    sp.add_argument("--kind", default="original")
    sp.add_argument("--platform", required=True)
    sp.add_argument("--material-id", default="")
    sp.add_argument("--backend-cmd", default="")

    sp = brand_cmd("queue", cmd_queue)
    sp.add_argument("action", choices=["list", "show", "approve", "edit"])
    sp.add_argument("draft_id", nargs="?")
    sp.add_argument("--file", default="")

    brand_cmd("funnel-check", cmd_funnel_check)

    sp = brand_cmd("publish", cmd_publish)
    sp.add_argument("--live", action="store_true",
                    help="v1 has no live sink; this will error by design")

    sp = sub.add_parser("digest")
    sp.add_argument("--brand", default="")
    sp.set_defaults(fn=cmd_digest)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
