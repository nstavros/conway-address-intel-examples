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
from .config.registry import RegistryError, load_registry
from .ceo.agent import (
    DirectiveRejected,
    EscalationRejected,
    business_objective,
    create_directive,
    create_escalation,
    detect_conflicts,
)
from .ceo.reports import build_ceo_daily_exceptions, build_ceo_weekly
from .engagement.agent import (
    EngagementRejected,
    approve_for_manual_posting,
    emit_engagement_insights,
    generate_reply_review,
    import_comments_batch,
    render_review_queue_item,
    route_lead_signal,
    route_risk_signal,
    transition_reply,
)
from .engagement.reports import build_engagement_daily_exceptions, build_engagement_weekly_summary
from .core.models import EXPERIMENT_STATUSES, Draft, LifecycleError, SourceMaterial, new_id
from .core.store import Store
from .drafting.originals import (
    approve_content_record,
    create_original_content,
    repurpose_for_registry,
    write_handoff,
)
from .drafting.replies import draft_replies
from .drafting.repurpose import repurpose as repurpose_material
from .funnel.classifier import FunnelViolation, check_week
from .ingest.chatplace import ChatplaceMediaSource
from .ingest.manual import ManualSource
from .ingest.youtube_rss import YouTubeRSSSource
from .measure import diagnosis as intel_diagnosis
from .measure.collector import attribute_signups, record_performance
from .measure.experiments import ExperimentValidationError, create_experiment
from .measure.intelligence import IngestRejected, ingest_performance_batch, write_recommendation
from .measure.reports import build_daily_exceptions, build_weekly_report
from .publish.operations import (
    PublishingRejected,
    attempt_publish,
    receive_handoff,
    retry_publish,
    verify_publication,
)
from .publish.publisher import DryRunSink, publish
from .publish.sinks import SINKS
from .report.digest import build_digest
from .report.weekly import build_weekly
from .triage.comments import mine_themes, triage_comments
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
    if args.source in ("manual", "chatplace") and not args.file:
        raise SystemExit(f"--file is required for source {args.source}")
    if args.source == "manual":
        src = ManualSource(ctx.name, args.file)
    elif args.source == "chatplace":
        src = ChatplaceMediaSource(ctx.name, args.file)
    elif args.source == "youtube":
        channel = (ctx.config.get("listening") or {}).get("youtube_channel_id", "")
        if not args.file and not channel:
            raise SystemExit("youtube source needs listening.youtube_channel_id "
                             "in the brand config, or --file with a saved feed XML")
        src = YouTubeRSSSource(ctx.name, channel_id=channel, path=args.file)
    else:
        raise SystemExit(f"unknown source {args.source}")
    posts = src.poll()
    for p in posts:
        store.upsert_post(p)
    print(f"[{ctx.name}] ingested {len(posts)} posts from "
          f"{args.file or 'live feed'}")


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


def cmd_repurpose(args):
    ctx, store = _load(args)
    material = store.get_material(args.material_id)
    if material is None:
        raise SystemExit(f"no material {args.material_id} — add with: engage material")
    backend = _cmd_backend(args.backend_cmd) if args.backend_cmd else _stub_backend
    llm = backend if args.backend_cmd else None
    platforms = args.platforms.split(",") if args.platforms else None
    drafts, skipped = repurpose_material(store, ctx, material, backend, platforms)
    for p in skipped:
        print(f"[{ctx.name}] {p}: skipped — material used there inside the reuse window")
    for d in drafts:
        submit(store, ctx, d, (material["text"],), llm)
        print(f"[{ctx.name}] draft {d.id} ({d.platform}) -> {d.status}")
        if d.gate_reasons:
            print(f"    gates: {d.gate_reasons}")
        print(f"    {d.text[:160]}")
    if not drafts and not skipped:
        print(f"[{ctx.name}] no eligible platforms")


def cmd_original(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    material = store.get_material(args.material_id)
    if material is None:
        raise SystemExit(f"no material {args.material_id} — add with: engage material")
    backend = _cmd_backend(args.backend_cmd) if args.backend_cmd else _stub_backend
    try:
        record = create_original_content(
            store, ctx, registry, material, args.platform, backend,
            audience=args.audience, objective=args.objective, cta=args.cta,
            pillar=args.pillar or None, format=args.format,
            lead_magnet=args.lead_magnet, success_metric=args.success_metric,
            allow_parked=args.allow_parked,
        )
    except (ValueError, LifecycleError) as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} ({record.platform}) "
          f"-> {record.lifecycle_status}  (draft {record.draft_id})")
    for ev in store.list_content_events(record.id):
        print(f"    {ev['event_type']}: {ev['detail']}")
    if args.repurpose:
        fanned = repurpose_for_registry(store, ctx, registry, material, backend,
                                        args.platform, allow_parked=args.allow_parked)
        for r in fanned:
            print(f"[{ctx.name}] repurposed content record {r.id} ({r.platform}) "
                  f"-> {r.lifecycle_status}  (draft {r.draft_id})")


def cmd_original_approve(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    try:
        record = approve_content_record(store, ctx, record, args.approver, args.note)
    except LifecycleError as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} -> approved by {args.approver}")


def cmd_original_handoff(args):
    ctx, store = _load(args)
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    payload = write_handoff(store, record, args.target)
    print(json.dumps(payload, indent=2))


def _load_sink(args):
    cls = SINKS.get(args.sink)
    if cls is None:
        raise SystemExit(f"unknown sink {args.sink!r} (choices: {sorted(SINKS)})")
    return cls()


def cmd_publish_schedule(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    # write_handoff() requires approved-or-later status by design (Phase 1
    # QC fix) — a record paused for unverifiable constraints doesn't
    # qualify, so resuming it constructs the minimal handoff shape directly
    # rather than relaxing write_handoff's own guarantee for every caller.
    if record.lifecycle_status == "paused":
        handoff = {"target": "publishing", "content_record_id": record.id}
    else:
        handoff = write_handoff(store, record, "publishing")
    sink = _load_sink(args)
    scheduled_at = args.scheduled_at if args.scheduled_at else int(time.time())
    try:
        record = receive_handoff(store, ctx, registry, handoff, sink=sink,
                                 scheduled_at=scheduled_at, timezone=args.timezone or None,
                                 constraints_override_reason=args.override_reason)
    except (PublishingRejected, LifecycleError) as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} -> {record.lifecycle_status} "
          f"(sink={record.publish_method or '-'}, job={record.sink_job_id or '-'})")


def cmd_publish_attempt(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    sink = _load_sink(args)
    try:
        record = attempt_publish(store, ctx, registry, record, sink)
    except PublishingRejected as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} -> {record.lifecycle_status} "
          f"(published_url={record.published_url or '(none — never fabricated)'}, "
          f"platform_post_id={record.platform_post_id or '(none — never fabricated)'})")
    if record.lifecycle_status == "scheduled" and args.sink == "manual_review":
        checklist = record.verification_evidence.get("checklist", {})
        print(json.dumps(checklist, indent=2))


def cmd_publish_verify(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    sink = _load_sink(args)
    try:
        record = verify_publication(store, ctx, registry, record, sink)
    except PublishingRejected as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} -> {record.lifecycle_status} "
          f"(verification_status={record.verification_status or '-'})")


def cmd_publish_retry(args):
    ctx, store = _load(args)
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    try:
        record = retry_publish(store, ctx, record, retry_limit=args.retry_limit)
    except (PublishingRejected, LifecycleError) as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] content record {record.id} -> {record.lifecycle_status} "
          f"(retry {record.retry_count}/{args.retry_limit})")


def cmd_publish_events(args):
    ctx, store = _load(args)
    record = store.get_content_record(args.record_id)
    if record is None or record.brand != ctx.name:
        raise SystemExit(f"no content record {args.record_id} for brand {ctx.name}")
    for e in store.list_content_events(record.id):
        print(f"{e['event_type']} | {e['actor']} | {json.dumps(e['detail'])}")


def cmd_measure_import(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
    accepted, rejected = ingest_performance_batch(store, ctx, registry, rows)
    print(f"[{ctx.name}] ingested {len(accepted)} performance row(s), rejected {len(rejected)}")
    for r in rejected:
        print(f"  REJECTED: {r['reason']} ({json.dumps(r['row'])[:120]})")


def cmd_measure_findings(args):
    ctx, store = _load(args)
    perf = store.list_performance_records(ctx.name)
    if args.kind == "publishing_or_coverage_failures":
        findings = intel_diagnosis.publishing_or_coverage_failures(perf, store.list_content_records(ctx.name))
    elif args.kind == "lead_quality_issues":
        findings = intel_diagnosis.lead_quality_issues(perf)
    else:
        findings = getattr(intel_diagnosis, args.kind)(perf, args.dimension)
    if not findings:
        print(f"[{ctx.name}] no findings ({args.kind}, dimension={args.dimension})")
        return
    for f in findings:
        print(f"[{f.label}, {f.confidence}] {f.summary}")
        if f.data_limitations:
            print(f"    limitations: {'; '.join(f.data_limitations)}")


def cmd_experiment_create(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    try:
        exp = create_experiment(
            store, ctx, registry, business_objective=args.business_objective, hypothesis=args.hypothesis,
            independent_variable=args.independent_variable, control=args.control, treatment=args.treatment,
            target_platform=args.target_platform, success_metric=args.success_metric,
            decision_rule=args.decision_rule,
        )
    except ExperimentValidationError as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] experiment {exp.id} -> {exp.status}")


def cmd_experiment_list(args):
    ctx, store = _load(args)
    for e in store.list_experiments(ctx.name, status=args.status or None):
        print(f"{e.id} [{e.status}] {e.hypothesis[:100]}")


def cmd_recommendation_write(args):
    ctx, store = _load(args)
    rec = write_recommendation(store, ctx.name, args.target, args.label, args.summary,
                               {"note": "cli-authored"}, args.confidence)
    print(json.dumps(rec, indent=2))


def cmd_weekly_intelligence(args):
    names = [args.brand] if args.brand else discover_brands(_brands_dir(args))
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    sections = []
    for name in names:
        ctx = load_brand(_brands_dir(args), name)
        sections.append((ctx, Store(ctx.db_path()), registry))
    print(build_weekly_report(sections))


def cmd_daily_exceptions(args):
    names = [args.brand] if args.brand else discover_brands(_brands_dir(args))
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    sections = []
    for name in names:
        ctx = load_brand(_brands_dir(args), name)
        sections.append((ctx, Store(ctx.db_path()), registry))
    print(build_daily_exceptions(sections))


def _ceo_sections(args):
    names = [args.brand] if args.brand else discover_brands(_brands_dir(args))
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    out = []
    for name in names:
        ctx = load_brand(_brands_dir(args), name)
        out.append((ctx, Store(ctx.db_path()), registry))
    return out


def cmd_ceo_weekly(args):
    print(build_ceo_weekly(_ceo_sections(args)))


def cmd_ceo_exceptions(args):
    print(build_ceo_daily_exceptions(_ceo_sections(args)))


def cmd_ceo_objective(args):
    ctx, _store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    print(json.dumps(business_objective(ctx, registry), indent=2))


def cmd_ceo_conflicts(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    conflicts = detect_conflicts(store, ctx, registry)
    if not conflicts:
        print(f"[{ctx.name}] no conflicts detected")
        return
    for c in conflicts:
        flag = "!" if c["requires_escalation"] else "-"
        print(f"{flag} [{c['kind']}] {c['detail']}  traces_to={c['traces_to']}")


def cmd_ceo_directive(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    try:
        d = create_directive(
            store, ctx, registry, business_objective=args.business_objective,
            audience=args.audience, platform_scope=args.platform_scope.split(","),
            campaign=args.campaign, owner_agent=args.owner_agent, deliverable=args.deliverable,
            deadline=args.deadline, success_metric=args.success_metric,
            approval_requirement=args.approval_requirement,
            exclusions=[e for e in args.exclusions.split(",") if e],
            traces_to=[t for t in args.traces_to.split(",") if t],
        )
    except DirectiveRejected as e:
        raise SystemExit(str(e))
    print(json.dumps(d, indent=2))


def cmd_ceo_escalate(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    try:
        esc = create_escalation(
            store, ctx, registry, trigger=args.trigger, issue=args.issue,
            why_it_matters=args.why_it_matters, recommended_action=args.recommended_action,
            alternatives=[a for a in args.alternatives.split("|") if a], deadline=args.deadline,
            consequence_of_no_decision=args.consequence,
            traces_to=[t for t in args.traces_to.split(",") if t],
        )
    except EscalationRejected as e:
        raise SystemExit(str(e))
    print(json.dumps(esc, indent=2))


def cmd_engagement_import(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
    accepted, rejected = import_comments_batch(store, ctx, registry, rows)
    print(f"[{ctx.name}] imported {len(accepted)} comment(s), rejected {len(rejected)}")
    for c in accepted:
        print(f"  {c.id} [{c.triage_class}, {c.triage_confidence:.2f}] {c.text[:80]}")
    for r in rejected:
        print(f"  REJECTED: {r['reason']}")


def cmd_engagement_reply(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    comment = store.get_comment(args.comment_id)
    if comment is None or comment.brand != ctx.name:
        raise SystemExit(f"no comment {args.comment_id} for brand {ctx.name}")
    backend = _cmd_backend(args.backend_cmd) if args.backend_cmd else _stub_backend
    try:
        replies = generate_reply_review(store, ctx, registry, comment, backend)
    except EngagementRejected as e:
        raise SystemExit(str(e))
    for r in replies:
        print(f"[{ctx.name}] reply {r.id} ({r.angle[:40]}) -> {r.review_status}")
        print(f"    {r.draft_text[:200]}")


def cmd_engagement_approve(args):
    ctx, store = _load(args)
    reply = store.get_reply_draft(args.reply_id)
    if reply is None or reply.brand != ctx.name:
        raise SystemExit(f"no reply draft {args.reply_id} for brand {ctx.name}")
    try:
        reply = approve_for_manual_posting(store, reply, args.approver)
    except EngagementRejected as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] reply {reply.id} -> {reply.review_status} (approver: {args.approver})")


def cmd_engagement_transition(args):
    ctx, store = _load(args)
    reply = store.get_reply_draft(args.reply_id)
    if reply is None or reply.brand != ctx.name:
        raise SystemExit(f"no reply draft {args.reply_id} for brand {ctx.name}")
    try:
        reply = transition_reply(store, reply, args.status, reason=args.reason)
    except EngagementRejected as e:
        raise SystemExit(str(e))
    print(f"[{ctx.name}] reply {reply.id} -> {reply.review_status}")


def cmd_engagement_queue(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    comment = store.get_comment(args.comment_id)
    if comment is None or comment.brand != ctx.name:
        raise SystemExit(f"no comment {args.comment_id} for brand {ctx.name}")
    print(json.dumps(render_review_queue_item(store, ctx, registry, comment), indent=2))


def cmd_engagement_route(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    comment = store.get_comment(args.comment_id)
    if comment is None or comment.brand != ctx.name:
        raise SystemExit(f"no comment {args.comment_id} for brand {ctx.name}")
    try:
        if args.kind == "risk":
            result = route_risk_signal(store, ctx, registry, comment)
        else:
            result = route_lead_signal(store, ctx, registry, comment)
    except EngagementRejected as e:
        raise SystemExit(str(e))
    print(json.dumps(result, indent=2) if result else "no escalation created (below routing threshold)")


def cmd_engagement_insights(args):
    ctx, store = _load(args)
    try:
        registry = load_registry(args.registry_dir or None)
    except RegistryError as e:
        raise SystemExit(str(e))
    print(json.dumps(emit_engagement_insights(store, ctx, registry), indent=2))


def cmd_engagement_weekly(args):
    print(build_engagement_weekly_summary(_ceo_sections(args)))


def cmd_engagement_exceptions(args):
    print(build_engagement_daily_exceptions(_ceo_sections(args)))


def cmd_triage(args):
    ctx, _store = _load(args)
    comments = json.loads(Path(args.file).read_text(encoding="utf-8"))
    results = triage_comments(comments, ctx)
    for cls in ("HUMAN", "DRAFTABLE", "SKIP"):
        group = [r for r in results if r.cls == cls]
        if not group:
            continue
        print(f"{cls} ({len(group)}):")
        for r in group:
            print(f"  @{r.author}: {r.text[:80]}")
            print(f"      -> {r.reason}")
    themes = mine_themes(comments, ctx)
    if themes:
        print("Themes the audience keeps raising:")
        for topic, n in themes:
            print(f"  {n}x {topic}")


def cmd_measure(args):
    ctx, store = _load(args)
    if args.signups:
        rows = json.loads(Path(args.signups).read_text(encoding="utf-8"))
        results = attribute_signups(store, ctx, rows)
        matched = [r for r in results if r["draft_id"]]
        print(f"[{ctx.name}] {len(matched)}/{len(results)} signups attributed")
        for r in results:
            target = f"{r['draft_id']} ({r['platform']})" if r["draft_id"] else "UNMATCHED"
            print(f"  code={r['code']} -> {target}")
    if args.file:
        rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
        report = record_performance(store, ctx, rows)
        print(f"[{ctx.name}] recorded {report['recorded']} metric values; "
              f"normalized {report['normalized']} posts on '{report['headline_metric']}'")
    if not args.file and not args.signups:
        raise SystemExit("pass --file metrics.json and/or --signups signups.json")


def cmd_weekly(args):
    ctx, store = _load(args)
    print(build_weekly(ctx, store))


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
    sp.add_argument("--source", choices=["manual", "chatplace", "youtube"], required=True)
    sp.add_argument("--file", default="",
                    help="required for manual/chatplace; optional for youtube "
                         "(defaults to fetching the configured channel's live feed)")

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

    sp = brand_cmd("repurpose", cmd_repurpose)
    sp.add_argument("--material-id", required=True)
    sp.add_argument("--backend-cmd", default="")
    sp.add_argument("--platforms", default="",
                    help="comma-separated subset; default: all brand platforms")

    sp = brand_cmd("original", cmd_original)
    sp.add_argument("--material-id", required=True)
    sp.add_argument("--platform", required=True)
    sp.add_argument("--audience", required=True)
    sp.add_argument("--objective", required=True)
    sp.add_argument("--cta", required=True)
    sp.add_argument("--pillar", default="")
    sp.add_argument("--format", default="reel",
                    choices=["reel", "carousel", "story-sequence", "lead-magnet"])
    sp.add_argument("--lead-magnet", default="none")
    sp.add_argument("--success-metric", default="views")
    sp.add_argument("--backend-cmd", default="")
    sp.add_argument("--registry-dir", default="",
                    help="override the harness registry path (default: "
                         "~/.claude/harness/social/registry)")
    sp.add_argument("--allow-parked", action="store_true",
                    help="required if the brand is marked deprioritized in the registry")
    sp.add_argument("--repurpose", action="store_true",
                    help="also fan out platform-native drafts to every other "
                         "registry-enabled, brand-configured platform")

    sp = brand_cmd("original-approve", cmd_original_approve)
    sp.add_argument("record_id")
    sp.add_argument("--approver", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("original-handoff", cmd_original_handoff)
    sp.add_argument("record_id")
    sp.add_argument("--target", required=True, choices=["publishing", "engagement", "performance"])

    sink_choices = ["dry_run", "manual_review", "api", "mcp", "scheduler", "browser"]

    sp = brand_cmd("publish-schedule", cmd_publish_schedule)
    sp.add_argument("record_id")
    sp.add_argument("--sink", default="dry_run", choices=sink_choices)
    sp.add_argument("--scheduled-at", type=int, default=0,
                    help="unix seconds; defaults to now")
    sp.add_argument("--timezone", default="",
                    help="defaults to the account registry's timezone, or America/New_York")
    sp.add_argument("--override-reason", default="",
                    help="required to proceed past an unknown/violated platform constraint")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("publish-attempt", cmd_publish_attempt)
    sp.add_argument("record_id")
    sp.add_argument("--sink", default="dry_run", choices=sink_choices)
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("publish-verify", cmd_publish_verify)
    sp.add_argument("record_id")
    sp.add_argument("--sink", default="dry_run", choices=sink_choices)
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("publish-retry", cmd_publish_retry)
    sp.add_argument("record_id")
    sp.add_argument("--retry-limit", type=int, default=3)

    sp = brand_cmd("publish-events", cmd_publish_events)
    sp.add_argument("record_id")

    sp = brand_cmd("triage", cmd_triage)
    sp.add_argument("--file", required=True,
                    help="JSON list of comments: [{id, author, text}]")

    sp = brand_cmd("measure", cmd_measure)
    sp.add_argument("--file", default="",
                    help="JSON metric rows: [{post_id, views: N, likes: N, ...}]")
    sp.add_argument("--signups", default="",
                    help="JSON signup rows: [{code, ts}]")

    brand_cmd("weekly", cmd_weekly)

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

    sp = brand_cmd("measure-import", cmd_measure_import)
    sp.add_argument("--file", required=True,
                    help="JSON list: [{content_record_id, metrics: {...}, source?, "
                         "attribution_status?, campaign?, creative_asset_id?}]")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("measure-findings", cmd_measure_findings)
    sp.add_argument("kind", choices=["repeated_winners", "repeated_underperformers",
                                     "high_reach_low_intent", "low_reach_high_intent",
                                     "publishing_or_coverage_failures", "lead_quality_issues"])
    sp.add_argument("--dimension", default="pillar", choices=list(intel_diagnosis.DIMENSIONS) + ["posting_window"])

    sp = brand_cmd("experiment-create", cmd_experiment_create)
    sp.add_argument("--business-objective", required=True)
    sp.add_argument("--hypothesis", required=True)
    sp.add_argument("--independent-variable", required=True)
    sp.add_argument("--control", required=True)
    sp.add_argument("--treatment", required=True)
    sp.add_argument("--target-platform", required=True)
    sp.add_argument("--success-metric", required=True)
    sp.add_argument("--decision-rule", required=True)
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("experiment-list", cmd_experiment_list)
    sp.add_argument("--status", default="", choices=[""] + list(EXPERIMENT_STATUSES))

    sp = brand_cmd("recommendation-write", cmd_recommendation_write)
    sp.add_argument("--target", required=True)
    sp.add_argument("--label", required=True)
    sp.add_argument("--summary", required=True)
    sp.add_argument("--confidence", default="low")

    sp = sub.add_parser("weekly-intelligence")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_weekly_intelligence)

    sp = sub.add_parser("daily-exceptions")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_daily_exceptions)

    sp = sub.add_parser("ceo-weekly")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_ceo_weekly)

    sp = sub.add_parser("ceo-exceptions")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_ceo_exceptions)

    sp = brand_cmd("ceo-objective", cmd_ceo_objective)
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("ceo-conflicts", cmd_ceo_conflicts)
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("ceo-directive", cmd_ceo_directive)
    sp.add_argument("--business-objective", required=True)
    sp.add_argument("--audience", required=True)
    sp.add_argument("--platform-scope", required=True, help="comma-separated")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--owner-agent", required=True,
                    choices=["planning_drafting", "publishing_operations",
                             "performance_intelligence", "engagement_future"])
    sp.add_argument("--deliverable", required=True)
    sp.add_argument("--deadline", required=True)
    sp.add_argument("--success-metric", required=True)
    sp.add_argument("--approval-requirement", required=True)
    sp.add_argument("--exclusions", default="", help="comma-separated")
    sp.add_argument("--traces-to", default="", help="comma-separated record ids")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("ceo-escalate", cmd_ceo_escalate)
    sp.add_argument("--trigger", required=True)
    sp.add_argument("--issue", required=True)
    sp.add_argument("--why-it-matters", required=True)
    sp.add_argument("--recommended-action", required=True)
    sp.add_argument("--alternatives", default="", help="pipe-separated")
    sp.add_argument("--deadline", required=True)
    sp.add_argument("--consequence", required=True)
    sp.add_argument("--traces-to", default="")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("engagement-import", cmd_engagement_import)
    sp.add_argument("--file", required=True,
                    help="JSON list: [{text, author, platform, source_type, source_id, external_id?}]")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("engagement-reply", cmd_engagement_reply)
    sp.add_argument("comment_id")
    sp.add_argument("--backend-cmd", default="")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("engagement-approve", cmd_engagement_approve)
    sp.add_argument("reply_id")
    sp.add_argument("--approver", required=True)

    sp = brand_cmd("engagement-transition", cmd_engagement_transition)
    sp.add_argument("reply_id")
    sp.add_argument("--status", required=True,
                    choices=["draft", "review_required", "rejected", "expired", "paused"])
    sp.add_argument("--reason", default="")

    sp = brand_cmd("engagement-queue", cmd_engagement_queue)
    sp.add_argument("comment_id")
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("engagement-route", cmd_engagement_route)
    sp.add_argument("comment_id")
    sp.add_argument("--kind", required=True, choices=["lead", "risk"])
    sp.add_argument("--registry-dir", default="")

    sp = brand_cmd("engagement-insights", cmd_engagement_insights)
    sp.add_argument("--registry-dir", default="")

    sp = sub.add_parser("engagement-weekly")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_engagement_weekly)

    sp = sub.add_parser("engagement-exceptions")
    sp.add_argument("--brand", default="")
    sp.add_argument("--registry-dir", default="")
    sp.set_defaults(fn=cmd_engagement_exceptions)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
