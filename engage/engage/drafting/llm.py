"""The single LLM chokepoint. Every generation call in the engine assembles
its prompt here, and assembly asserts brand isolation on every call: the
brand's own canary must be present and no foreign canary may appear. A foreign
canary raises — it never warns — because it means prompt assembly crossed
brands.

Backends are plain callables (prompt: str) -> str, injected by the CLI, so
drafting is testable offline with a stub."""
from __future__ import annotations

from typing import Callable

from ..core.brand import BrandContext, IsolationError


class PromptAssemblyError(Exception):
    pass


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown placeholders visible, don't crash
        return "{" + key + "}"


def assemble(ctx: BrandContext, prompt_name: str, variables: dict) -> str:
    if prompt_name not in ctx.prompts:
        raise PromptAssemblyError(
            f"brand '{ctx.name}' has no prompt '{prompt_name}' "
            f"(available: {sorted(ctx.prompts)})"
        )
    template = ctx.prompts[prompt_name]
    if ctx.canary not in template:
        raise IsolationError(f"prompt '{prompt_name}' lost its canary for brand '{ctx.name}'")
    text = template.format_map(_SafeDict(variables))
    ctx.assert_no_foreign_canary(text)
    for value in variables.values():
        if isinstance(value, str):
            ctx.assert_no_foreign_canary(value)
    return text


def complete(ctx: BrandContext, prompt_name: str, variables: dict,
             backend: Callable[[str], str]) -> str:
    prompt = assemble(ctx, prompt_name, variables)
    out = backend(prompt)
    ctx.assert_no_foreign_canary(out)
    # strip any canary echo before the text can reach a draft
    return out.replace(ctx.canary, "").strip()
