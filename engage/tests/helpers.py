from __future__ import annotations

from pathlib import Path

from engage.config import discover_brands, load_brand
from engage.core.store import Store

ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = ROOT / "brands"
PKG_DIR = ROOT / "engage"
FIXTURES = ROOT / "fixtures"


def all_contexts():
    return [load_brand(BRANDS_DIR, n) for n in discover_brands(BRANDS_DIR)]


def ctx_for(name):
    return load_brand(BRANDS_DIR, name)


def mem_store():
    return Store(":memory:")


def pass_llm(prompt: str) -> str:
    return "PASS"


def block_llm(prompt: str) -> str:
    return "BLOCK: violates brand rules"


def broken_llm(prompt: str) -> str:
    raise RuntimeError("judge unavailable")
