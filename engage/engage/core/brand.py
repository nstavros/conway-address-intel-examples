"""BrandContext — the single isolation boundary.

Every module that touches content takes a BrandContext. It is constructed
only by engage.config.loader from exactly one brands/<name>/ directory.
There is no global config and no way to hold two brands in one context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CANARY_RE = re.compile(r"\[\[CANARY:([a-z0-9_-]+)\]\]")


def canary_for(brand: str) -> str:
    return f"[[CANARY:{brand}]]"


class IsolationError(Exception):
    """Raised when content from one brand crosses into another's pipeline."""


@dataclass
class BrandContext:
    name: str
    root: Path
    config: dict
    prompts: dict[str, str]
    compiled_blocks: list[tuple[str, re.Pattern]] = field(default_factory=list)

    @property
    def canary(self) -> str:
        return canary_for(self.name)

    def assert_no_foreign_canary(self, text: str) -> None:
        for match in CANARY_RE.finditer(text):
            if match.group(1) != self.name:
                raise IsolationError(
                    f"foreign canary '{match.group(0)}' found in content for brand "
                    f"'{self.name}' — cross-brand contamination, refusing to continue"
                )

    def db_path(self) -> Path:
        data = self.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        return data / "engage.db"
