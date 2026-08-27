"""Adapter for exports from the Chatplace MCP connector.

Parses the JSON shape returned by `automations_triggers_list_instagram_media`
(saved to a file): a list of {mediaId, caption, permalink, timestamp, ...}.
These are OUR OWN posts, so they land with own=True — they feed measure,
repurposing, and comment triage, never the opportunity ranker."""
from __future__ import annotations

import json
from pathlib import Path

from ..core.models import Post


class ChatplaceMediaSource:
    def __init__(self, brand: str, path: Path | str, author: str = ""):
        self.brand = brand
        self.path = Path(path)
        self.author = author

    def poll(self) -> list[Post]:
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            Post(
                id=str(r["mediaId"]),
                brand=self.brand,
                platform=r.get("platform", "instagram"),
                author=r.get("author", self.author) or "self",
                text=r.get("caption", ""),
                url=r.get("permalink", ""),
                created_at=int(r.get("timestamp", 0)),
                own=True,
            )
            for r in rows
        ]
