"""Strict validation for brand YAML. Unknown top-level keys are rejected so a
typo can't silently disable a safety rule."""
from __future__ import annotations


class BrandConfigError(Exception):
    pass


ALLOWED_TOP = {
    "brand", "priority", "platforms", "watch", "voice", "scoring", "blocks",
    "funnel", "cadence", "rate_limits", "listening",
}
REQUIRED_TOP = {"brand", "platforms", "scoring", "blocks", "voice"}
SCORE_COMPONENTS = {"relevance", "author_value", "recency", "momentum", "history"}


def validate(raw: dict, brand_name: str) -> dict:
    if not isinstance(raw, dict):
        raise BrandConfigError(f"{brand_name}: config must be a mapping")
    problems: list[str] = []

    unknown = set(raw) - ALLOWED_TOP
    if unknown:
        problems.append(f"unknown top-level keys: {sorted(unknown)}")
    missing = REQUIRED_TOP - set(raw)
    if missing:
        problems.append(f"missing required keys: {sorted(missing)}")

    if raw.get("brand") != brand_name:
        problems.append(f"'brand' must equal directory name '{brand_name}'")

    platforms = raw.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        problems.append("'platforms' must be a non-empty list")

    scoring = raw.get("scoring") or {}
    weights = scoring.get("weights") or {}
    if set(weights) != SCORE_COMPONENTS:
        problems.append(f"scoring.weights must define exactly {sorted(SCORE_COMPONENTS)}")
    else:
        total = sum(weights.values())
        if abs(total - 1.0) > 0.001:
            problems.append(f"scoring.weights must sum to 1.0 (got {total})")

    blocks = raw.get("blocks")
    if not isinstance(blocks, dict):
        problems.append("'blocks' must be a mapping (use empty lists to opt out explicitly)")
    else:
        if "regex" not in blocks or not isinstance(blocks["regex"], list):
            problems.append("blocks.regex must be a list (may be empty, but must exist)")
        if blocks.get("llm_gate", "required") not in ("required", "off"):
            problems.append("blocks.llm_gate must be 'required' or 'off'")

    voice = raw.get("voice") or {}
    if not isinstance(voice.get("signature_phrases", []), list):
        problems.append("voice.signature_phrases must be a list")

    funnel = raw.get("funnel")
    if funnel is not None:
        ratio = funnel.get("ratio") or {}
        if set(ratio) != {"top", "middle", "bottom"}:
            problems.append("funnel.ratio must define top, middle, bottom")
        elif abs(sum(ratio.values()) - 1.0) > 0.001:
            problems.append("funnel.ratio must sum to 1.0")

    if problems:
        raise BrandConfigError(f"{brand_name}: " + "; ".join(problems))

    # defaults
    scoring.setdefault("question_multiplier", 1.0)
    scoring.setdefault("cooldown_days", 7)
    scoring.setdefault("half_life_hours", {})
    scoring.setdefault("momentum_baseline_per_hour", 25)
    raw.setdefault("listening", {})
    raw["listening"].setdefault("reuse_window_days", 45)
    blocks.setdefault("llm_gate", "required")
    blocks.setdefault("numeric_provenance", False)
    blocks.setdefault("claims_require_source", False)
    return raw
