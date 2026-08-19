"""The approval queue — the ONLY write path toward publishing.

- submit() runs the safety gates; a draft that fails lands as BLOCKED and
  cannot be approved (there is no override).
- approve() binds approval to the SHA-256 of the exact draft text.
- edit_draft() replaces the text, revokes any approval, and resets the draft
  to DRAFT so it must pass gates again.
"""
from __future__ import annotations

from typing import Callable

from ..core.brand import BrandContext
from ..core.models import Draft
from ..core.store import Store
from ..funnel.classifier import classify
from ..safety.gates import run_gates


class ApprovalError(Exception):
    pass


def submit(store: Store, ctx: BrandContext, draft: Draft,
           source_texts: tuple[str, ...] = (),
           llm: Callable[[str], str] | None = None) -> Draft:
    result = run_gates(draft.text, ctx, source_texts, llm)
    draft.funnel_class = classify(draft.text, ctx)
    draft.status = "PENDING" if result.passed else "BLOCKED"
    draft.gate_reasons = result.reasons
    store.save_draft(draft)
    if draft.material_id:
        store.record_material_use(draft.material_id, draft.id, draft.platform)
    return draft


def approve(store: Store, ctx: BrandContext, draft_id: str) -> Draft:
    draft = store.get_draft(draft_id, ctx.name)
    if draft is None:
        raise ApprovalError(f"no draft {draft_id}")
    if draft.status == "BLOCKED":
        raise ApprovalError(
            f"draft {draft_id} is BLOCKED by safety gates and cannot be approved: "
            f"{draft.gate_reasons}"
        )
    if draft.status != "PENDING":
        raise ApprovalError(f"draft {draft_id} is {draft.status}, not PENDING")
    store.record_approval(draft_id, draft.hash)
    store.set_draft_status(draft_id, "APPROVED")
    draft.status = "APPROVED"
    return draft


def edit_draft(store: Store, ctx: BrandContext, draft_id: str, new_text: str) -> Draft:
    draft = store.get_draft(draft_id, ctx.name)
    if draft is None:
        raise ApprovalError(f"no draft {draft_id}")
    draft.text = new_text
    draft.status = "DRAFT"
    draft.gate_reasons = []
    store.revoke_approval(draft_id)
    store.save_draft(draft)
    return draft
