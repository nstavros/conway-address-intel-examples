"""Cross-brand contamination checks, run by the test suite (and available to
CI). Two mechanisms beyond the canary assertions built into the pipeline:

- signature scan: text generated for brand A must not contain brand B's
  signature phrases (from each brand's voice.signature_phrases)
- engine purity: the engage package source must contain no brand names or
  signature phrases at all
"""
from __future__ import annotations

from pathlib import Path

from ..core.brand import BrandContext


def foreign_signatures(text: str, ctx: BrandContext, all_contexts: list[BrandContext]) -> list[str]:
    hits = []
    lowered = text.lower()
    for other in all_contexts:
        if other.name == ctx.name:
            continue
        for phrase in other.config["voice"].get("signature_phrases", []):
            if phrase.lower() in lowered:
                hits.append(f"signature of brand '{other.name}' found: {phrase!r}")
    return hits


def engine_purity_violations(package_dir: Path | str, forbidden_terms: list[str]) -> list[str]:
    violations = []
    for py in Path(package_dir).rglob("*.py"):
        source = py.read_text(encoding="utf-8").lower()
        for term in forbidden_terms:
            if term.lower() in source:
                violations.append(f"{py}: contains forbidden brand term {term!r}")
    return violations
