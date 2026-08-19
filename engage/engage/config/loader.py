"""Loads brands/<name>/ into a BrandContext, enforcing isolation at load time:

- config validated strictly (schema.py); invalid regex is a config error
- prompt files must live inside the brand directory (path-checked after resolve)
- every prompt file must carry the brand's own canary token
- a foreign canary anywhere in config or prompts fails the load
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..core.brand import BrandContext, IsolationError, canary_for
from .schema import BrandConfigError, validate


def discover_brands(brands_dir: Path | str) -> list[str]:
    root = Path(brands_dir)
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "config.yaml").exists()
    ) if root.exists() else []


def load_brand(brands_dir: Path | str, name: str) -> BrandContext:
    root = (Path(brands_dir) / name).resolve()
    cfg_path = root / "config.yaml"
    if not cfg_path.exists():
        raise BrandConfigError(f"{name}: no config.yaml under {root}")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    config = validate(raw, name)

    prompt_dir = (root / config["voice"].get("prompt_dir", "prompts")).resolve()
    if not prompt_dir.is_relative_to(root):
        raise IsolationError(f"{name}: prompt_dir resolves outside the brand directory")
    if not prompt_dir.is_dir():
        raise BrandConfigError(f"{name}: prompt dir {prompt_dir} does not exist")

    canary = canary_for(name)
    prompts: dict[str, str] = {}
    for f in sorted(prompt_dir.glob("*.md")):
        resolved = f.resolve()
        if not resolved.is_relative_to(root):
            raise IsolationError(f"{name}: prompt file {f} resolves outside the brand directory")
        text = resolved.read_text(encoding="utf-8")
        if canary not in text:
            raise IsolationError(f"{name}: prompt {f.name} is missing its canary token {canary}")
        prompts[f.stem] = text

    compiled: list[tuple[str, re.Pattern]] = []
    for pattern in config["blocks"]["regex"]:
        try:
            compiled.append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error as e:
            raise BrandConfigError(f"{name}: invalid block regex {pattern!r}: {e}") from e

    ctx = BrandContext(name=name, root=root, config=config, prompts=prompts,
                       compiled_blocks=compiled)
    # a foreign canary in this brand's own files is contamination at rest
    for text in prompts.values():
        ctx.assert_no_foreign_canary(text)
    return ctx
