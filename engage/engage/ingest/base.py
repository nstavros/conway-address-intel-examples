"""Ingest protocol. Everything downstream consumes Post objects; nothing
downstream knows whether a Post came from an API, an MCP connector export, or
pasted text. APIs are assumed flaky — ManualSource is a first-class citizen."""
from __future__ import annotations

from typing import Iterable, Protocol

from ..core.models import Post


class IngestSource(Protocol):
    def poll(self) -> Iterable[Post]: ...
