"""YouTube channel ingest via the public RSS feed — no API key, no OAuth.

https://www.youtube.com/feeds/videos.xml?channel_id=UC... returns the channel's
last ~15 uploads including per-video view counts (media:statistics). These are
OUR OWN posts (own=True): they feed measure, repurposing, and triage, never the
opportunity ranker. Each ingest appends a fresh (timestamp, views) engagement
snapshot; two or more snapshots enable momentum.

The channel id comes from brand config (listening.youtube_channel_id) or a
saved XML file — the engine carries no channel identity of its own."""
from __future__ import annotations

import ssl
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..core.models import Post

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class YouTubeRSSSource:
    def __init__(self, brand: str, channel_id: str = "", path: Path | str = ""):
        if not channel_id and not path:
            raise ValueError("YouTubeRSSSource needs a channel_id or a saved feed file")
        self.brand = brand
        self.channel_id = channel_id
        self.path = Path(path) if path else None

    def _feed_xml(self) -> str:
        if self.path:
            return self.path.read_text(encoding="utf-8")
        req = urllib.request.Request(
            FEED_URL.format(channel_id=self.channel_id),
            headers={"User-Agent": "engage-ingest"},
        )
        # python.org builds on macOS often lack system CA linkage; fall back
        # to certifi's bundle rather than failing (never disable verification)
        try:
            context = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=30, context=context) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise
            try:
                import certifi
            except ImportError:
                raise RuntimeError(
                    "TLS certificates unavailable — run 'pip install certifi' or "
                    "macOS 'Install Certificates.command', or pass --file with a "
                    "saved feed XML"
                ) from e
            context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(req, timeout=30, context=context) as resp:
                return resp.read().decode("utf-8")

    def poll(self) -> list[Post]:
        root = ET.fromstring(self._feed_xml())
        now = int(time.time())
        posts = []
        for entry in root.findall("atom:entry", NS):
            video_id = entry.findtext("yt:videoId", "", NS)
            if not video_id:
                continue
            title = entry.findtext("atom:title", "", NS)
            group = entry.find("media:group", NS)
            description = group.findtext("media:description", "", NS) if group is not None else ""
            stats = group.find("media:community/media:statistics", NS) if group is not None else None
            views = int(stats.get("views", 0)) if stats is not None else 0
            link = entry.find("atom:link[@rel='alternate']", NS)
            published = entry.findtext("atom:published", "", NS)
            try:
                created = int(datetime.fromisoformat(published).timestamp())
            except ValueError:
                created = 0
            posts.append(Post(
                id=video_id,
                brand=self.brand,
                platform="youtube",
                author="self",
                text=(title + "\n\n" + description).strip(),
                url=link.get("href", "") if link is not None else "",
                created_at=created,
                snapshots=[(now, views)],
                own=True,
            ))
        return posts
