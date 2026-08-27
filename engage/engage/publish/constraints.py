"""Platform-constraint structure and check. Every numeric/rule value below
is `None` — UNKNOWN — because no authorized connector or API has supplied
real capability data yet. Do not fill these in from memory, a blog post, or
a guess: they must come from an authorized, current source when Phase 2's
successor wires a real connector. Until then, every constraint check that
depends on one of these values fails closed (see check_constraints below) —
never truncates, reformats, or silently alters content, and never treats
"unknown" as "no limit".

An UNRECOGNIZED FORMAT is itself an unknown, and fails closed the same way.
REQUIRED_KEYS_BY_FORMAT doubles as the format allowlist for exactly this
reason: "I don't know which limits apply to this content" and "I don't know
what this limit is" are the same class of ignorance, and neither may be
resolved in favour of publishing."""
from __future__ import annotations

from dataclasses import dataclass, field

# UNKNOWN placeholders only. Keys are the constraints a real integration
# will eventually need; values stay None until that integration provides
# them from its own authorized capability data.
PLATFORM_CONSTRAINTS: dict[str, dict[str, int | None]] = {
    "instagram": {"caption_character_limit": None, "hashtag_limit": None, "media_required": None},
    "tiktok": {"caption_character_limit": None, "hashtag_limit": None, "media_required": None},
    "youtube": {"title_character_limit": None, "description_character_limit": None, "media_required": None},
    "linkedin": {"caption_character_limit": None, "hashtag_limit": None, "media_required": None},
    "x": {"caption_character_limit": None, "hashtag_limit": None, "media_required": None},
}

# Which of the above keys actually apply to a given brief's format — a
# text-only "reel" caption doesn't need a title_character_limit check, etc.
# Still returns UNKNOWN (never a guessed number) for any key it does check.
#
# This table is also the FORMAT ALLOWLIST: a format that does not appear here
# fails closed (see check_constraints). Adding a format here without also
# deciding which keys it requires is therefore a deliberate act, not an
# accident of omission.
REQUIRED_KEYS_BY_FORMAT: dict[str, tuple[str, ...]] = {
    "reel": ("caption_character_limit",),
    "carousel": ("caption_character_limit",),
    "story-sequence": ("caption_character_limit",),
    "lead-magnet": ("caption_character_limit",),
    # A YouTube Short carries a title and a description rather than a single
    # caption, and cannot exist without a media file — hence a different key
    # set from "reel". Every value these keys resolve to is still None/UNKNOWN
    # in PLATFORM_CONSTRAINTS above: naming the format here declares WHICH
    # limits must be known before publishing, never what those limits are.
    # There is no "make this a Short" switch on YouTube's side either —
    # Shorts classification is derived by the platform from the media itself,
    # so this format asserts intent and required checks, nothing more.
    "short": ("title_character_limit", "description_character_limit", "media_required"),
}


@dataclass
class ConstraintResult:
    ok: bool
    unknown: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def check_constraints(platform: str, brief: dict) -> ConstraintResult:
    """Never truncates, reformats, or alters `brief` in any way — a pure
    check. `ok=False` with a populated `unknown` list means "cannot verify
    this is safe to publish," which the caller (operations.py) must treat
    the same as a real violation: fail closed to review_required or paused,
    never proceed as if the check had passed."""
    table = PLATFORM_CONSTRAINTS.get(platform)
    if table is None:
        return ConstraintResult(ok=False, unknown=[f"no constraint table for platform {platform!r}"])

    # Fail closed on an unrecognized, unsupported, or missing format.
    # Previously this used REQUIRED_KEYS_BY_FORMAT.get(fmt, ()), which
    # resolved an unknown format to an empty required-key tuple — the loop
    # below then never ran and the function returned ok=True, so an
    # unrecognized format (or a brief with no format at all) silently
    # bypassed every constraint check and went straight to scheduling. That
    # is the exact opposite of this module's stated contract. An unknown
    # format is now an UNKNOWN like any other: not verifiable, not ok.
    fmt = brief.get("format", "")
    if fmt not in REQUIRED_KEYS_BY_FORMAT:
        return ConstraintResult(ok=False, unknown=[
            f"format {fmt!r} is not a recognized format — cannot determine which "
            f"{platform} constraints apply, so this content cannot be verified as "
            f"safe to publish (known formats: {sorted(REQUIRED_KEYS_BY_FORMAT)})"
        ])

    required = REQUIRED_KEYS_BY_FORMAT[fmt]
    unknown, violations = [], []
    for key in required:
        value = table.get(key)
        if value is None:
            unknown.append(f"{platform}.{key} is unknown — no authorized source has supplied it yet")
            continue
        # only reachable once a real integration replaces a None with a
        # real number; today this branch never executes.
        if key.endswith("_character_limit") and len(brief.get("caption", "")) > value:
            violations.append(f"{platform}.{key}: caption is {len(brief.get('caption', ''))} chars, limit {value}")

    return ConstraintResult(ok=not unknown and not violations, unknown=unknown, violations=violations)
