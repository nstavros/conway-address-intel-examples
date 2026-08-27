"""Daily digest: readable in two minutes, segmented by brand.
For each brand: top opportunities (with score components), pending drafts
awaiting approval, and anything blocked that needs a human decision."""
from __future__ import annotations

from ..core.brand import BrandContext
from ..core.store import Store


def _fmt_components(components: dict) -> str:
    return ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                     for k, v in components.items())


def build_digest(sections: list[tuple[BrandContext, Store]], top_n: int = 5) -> str:
    lines: list[str] = ["ENGAGE DAILY DIGEST", "=" * 40]
    for ctx, store in sections:
        lines.append(f"\n## {ctx.name.upper()}")

        opps = store.top_opportunities(ctx.name, top_n)
        if opps:
            lines.append("Top engagement opportunities:")
            for i, (o, p) in enumerate(opps, 1):
                head = p.text.splitlines()[0][:90] if p.text else "(no text)"
                lines.append(f"  {i}. [{o.score:.2f}] {p.author} on {p.platform}: {head}")
                lines.append(f"     {p.url or '(no url)'}  ({_fmt_components(o.components)})")
        else:
            lines.append("No scored opportunities. Run: engage ingest + engage score")

        pending = store.list_drafts(ctx.name, status="PENDING")
        if pending:
            lines.append(f"Drafts awaiting your approval: {len(pending)}")
            for d in pending[:top_n]:
                label = f"{d.kind}/{d.platform}" + (f" [{d.funnel_class}]" if d.funnel_class else "")
                lines.append(f"  - {d.id} ({label}) {d.text.splitlines()[0][:80]}")

        blocked = store.list_drafts(ctx.name, status="BLOCKED")
        if blocked:
            lines.append(f"NEEDS YOU — blocked by safety gates: {len(blocked)}")
            for d in blocked[:top_n]:
                lines.append(f"  - {d.id}: {'; '.join(d.gate_reasons)[:140]}")

        if not opps and not pending and not blocked:
            lines.append("(quiet)")
    return "\n".join(lines)
