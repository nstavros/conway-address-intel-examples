"""Repurposing fan-out: one piece of source material becomes platform-native
variants, one per platform. The reuse ledger enforces "one shot, one use":
a material already used on a platform inside the brand's configured
reuse window (listening.reuse_window_days) is skipped, never recycled.

Drafting stays offline-testable: the backend is an injected callable and the
material comes from the store — no network handles here."""
from __future__ import annotations

from typing import Callable

from ..core.brand import BrandContext
from ..core.models import Draft, new_id
from ..core.store import Store
from .llm import complete


def eligible_platforms(store: Store, ctx: BrandContext, material_id: str,
                       platforms: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Split candidate platforms into (eligible, skipped-by-reuse-ledger)."""
    window = ctx.config["listening"]["reuse_window_days"]
    candidates = platforms or list(ctx.config["platforms"])
    unknown = [p for p in candidates if p not in ctx.config["platforms"]]
    if unknown:
        raise ValueError(f"platforms not configured for this brand: {unknown}")
    eligible, skipped = [], []
    for p in candidates:
        (skipped if store.material_used_on(material_id, p, window) else eligible).append(p)
    return eligible, skipped


def repurpose(store: Store, ctx: BrandContext, material, backend: Callable[[str], str],
              platforms: list[str] | None = None) -> tuple[list[Draft], list[str]]:
    """Generate one platform-native draft per eligible platform.

    Returns (drafts, skipped_platforms). Drafts are NOT submitted here —
    the caller pushes each through the approval queue (the only write path),
    which also records the material use in the ledger."""
    eligible, skipped = eligible_platforms(store, ctx, material["id"], platforms)
    drafts = []
    for platform in eligible:
        text = complete(ctx, "repurpose", {
            "platform": platform,
            "title": material["title"],
            "material": material["text"],
            "kind": material["kind"],
            "voice": ctx.prompts.get("voice", ""),
        }, backend)
        drafts.append(Draft(
            id=new_id(), brand=ctx.name, kind="repurpose", platform=platform,
            text=text, material_id=material["id"],
        ))
    return drafts, skipped
