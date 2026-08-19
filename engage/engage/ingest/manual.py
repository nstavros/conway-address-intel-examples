"""Manual ingest: a JSON file of posts (list of objects). Minimal keys:
author, text. Optional: id, platform, url, created_at, engagement, own."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..core.models import Post, new_id


class ManualSource:
    def __init__(self, brand: str, path: Path | str, default_platform: str = "manual"):
        self.brand = brand
        self.path = Path(path)
        self.default_platform = default_platform

    def poll(self) -> list[Post]:
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        now = int(time.time())
        posts = []
        for r in rows:
            eng = r.get("engagement")
            posts.append(Post(
                id=str(r.get("id") or new_id()),
                brand=self.brand,
                platform=r.get("platform", self.default_platform),
                author=r.get("author", "unknown"),
                text=r.get("text", ""),
                url=r.get("url", ""),
                created_at=int(r.get("created_at") or now),
                snapshots=[(now, int(eng))] if eng is not None else [],
                own=bool(r.get("own", False)),
            ))
        return posts
