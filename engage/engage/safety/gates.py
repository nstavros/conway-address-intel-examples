"""Safety gates. Every draft passes here before it may enter the approval
queue. FAIL CLOSED: any error, timeout, or non-explicit-PASS from the LLM
layer blocks the draft. There is no override flag.

Layers:
1. regex   — deterministic per-brand rules from config (blocks.regex)
2. meta    — brand-neutral: text that is an assistant refusal or AI meta-text
   rather than a post (a backend refusal must never reach the queue)
3. provenance — numbers (and era-years) in the draft must literally appear in
   the linked source material (blocks.numeric_provenance /
   blocks.claims_require_source)
4. llm     — a judge callable(prompt)->str must answer starting with PASS
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..core.brand import BrandContext

# money ($74M, $1,200), percents (18%), multiples (1.6x, 2x), era years
# (216 BC / 476 AD), and large bare numbers (80,000 / 376)
NUMERIC_TOKEN_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:[MKBmkb]|million|billion)?"
    r"|\b\d+(?:\.\d+)?\s?%"
    r"|\b\d+(?:\.\d+)?\s?[x×]\b"
    r"|\b\d{3,4}\s?(?:BC|BCE|AD|CE)\b"
    r"|\b\d{1,3}(?:,\d{3})+\b"
    r"|\b\d{3,}\b"
)


def _normalize_num(tok: str) -> str:
    return re.sub(r"[\s,]", "", tok.lower()).replace("×", "x").replace("million", "m").replace("billion", "b")


# Assistant refusals / AI meta-text. A backend can decline a prompt; that
# refusal is well-formed text with no figures and no blocked terms, so no
# other layer catches it. Patterns are deliberately narrow to avoid blocking
# legitimate first-person copy ("I can't stress this enough" passes).
META_TEXT_RES = (
    re.compile(r"^\s*I(?:'m| am)? (?:sorry|apolog)", re.IGNORECASE),
    re.compile(
        r"\bI (?:can(?:no|')t|cannot|won'?t|am unable to|'m unable to) "
        r"(?:write|draft|generate|create|produce|help|assist|comply)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bas an AI\b|\bAI (?:assistant|model)\b|\blanguage model\b", re.IGNORECASE),
    re.compile(r"\bimpersonat(?:e|ing|ion)\b", re.IGNORECASE),
    # commentary about the prompt machinery instead of a post ("I don't have
    # any source material provided in this conversation" — observed live)
    re.compile(
        r"\bI (?:don'?t|do not) have (?:any )?(?:source material|voice guide|context|access)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bin this (?:conversation|prompt|context)\b", re.IGNORECASE),
    re.compile(r"\b(?:voice guide|system prompt)\b", re.IGNORECASE),
)


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _regex_layer(text: str, ctx: BrandContext) -> list[str]:
    reasons = []
    for pattern, rx in ctx.compiled_blocks:
        m = rx.search(text)
        if m:
            reasons.append(f"regex block {pattern!r} matched: {m.group(0)!r}")
    return reasons


def _meta_text_layer(text: str) -> list[str]:
    reasons = []
    for rx in META_TEXT_RES:
        m = rx.search(text)
        if m:
            reasons.append(f"meta-text block (assistant refusal, not a post): {m.group(0)!r}")
    return reasons


def _provenance_layer(text: str, ctx: BrandContext, source_texts: tuple[str, ...]) -> list[str]:
    blocks = ctx.config["blocks"]
    if not (blocks["numeric_provenance"] or blocks["claims_require_source"]):
        return []
    tokens = NUMERIC_TOKEN_RE.findall(text)
    if not tokens:
        return []
    if not source_texts:
        return [f"draft contains figures {tokens[:5]} but no source material is linked (fail closed)"]
    corpus = _normalize_num(" ".join(source_texts))
    missing = [t for t in tokens if _normalize_num(t) not in corpus]
    if missing:
        return [f"figures not found in source material (possibly invented): {sorted(set(missing))}"]
    return []


def _llm_layer(text: str, ctx: BrandContext, source_texts: tuple[str, ...],
               llm: Callable[[str], str] | None) -> list[str]:
    if ctx.config["blocks"]["llm_gate"] == "off":
        return []
    if llm is None:
        return ["llm gate required but no judge available (fail closed)"]
    topics = ctx.config["blocks"].get("topics", [])
    prompt = (
        "You are a compliance gate for social media drafts. Answer with exactly "
        "'PASS' or 'BLOCK: <reason>'.\n"
        f"Blocked topics/failure modes for this brand: {topics}\n"
        + (f"Source material the draft may draw on:\n{chr(10).join(source_texts)}\n" if source_texts else "")
        + f"Draft:\n{text}\n"
        "Block if the draft violates any blocked topic, asserts facts absent from "
        "the source material, or makes promises/claims the brand must not make."
    )
    try:
        verdict = (llm(prompt) or "").strip()
    except Exception as e:  # fail closed on any judge failure
        return [f"llm gate error (fail closed): {e}"]
    if verdict.upper().startswith("PASS"):
        return []
    return [f"llm gate blocked: {verdict[:300] or 'empty verdict (fail closed)'}"]


def run_gates(text: str, ctx: BrandContext, source_texts: tuple[str, ...] = (),
              llm: Callable[[str], str] | None = None) -> GateResult:
    ctx.assert_no_foreign_canary(text)
    reasons = _regex_layer(text, ctx)
    reasons += _meta_text_layer(text)
    reasons += _provenance_layer(text, ctx, source_texts)
    reasons += _llm_layer(text, ctx, source_texts, llm)
    return GateResult(passed=not reasons, reasons=reasons)
