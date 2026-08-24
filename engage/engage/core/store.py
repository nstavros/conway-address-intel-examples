"""Per-brand SQLite store. One DB file per brand, bound at construction.

There is deliberately no cross-brand query surface: a Store instance knows one
database file and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Draft, Opportunity, Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY, platform TEXT, author TEXT, text TEXT, url TEXT,
    created_at INTEGER, snapshots TEXT, own INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS opportunities (
    post_id TEXT PRIMARY KEY, score REAL, components TEXT,
    excluded INTEGER DEFAULT 0, exclusion_reason TEXT DEFAULT '',
    scored_at INTEGER
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, kind TEXT, platform TEXT, text TEXT,
    post_id TEXT DEFAULT '', material_id TEXT DEFAULT '',
    funnel_class TEXT DEFAULT '', status TEXT, gate_reasons TEXT DEFAULT '[]',
    angle TEXT DEFAULT '', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS approvals (
    draft_id TEXT PRIMARY KEY, content_hash TEXT, approved_at INTEGER
);
CREATE TABLE IF NOT EXISTS published (
    draft_id TEXT PRIMARY KEY, platform TEXT, published_at INTEGER, external_url TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS touches (
    author TEXT, platform TEXT, touched_at INTEGER, post_id TEXT
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY, title TEXT, text TEXT, kind TEXT, added_at INTEGER
);
CREATE TABLE IF NOT EXISTS material_uses (
    material_id TEXT, draft_id TEXT, platform TEXT, used_at INTEGER
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id TEXT, metric TEXT, value REAL, measured_at INTEGER
);
"""


class Store:
    def __init__(self, db_path: Path | str):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- posts -------------------------------------------------------------
    def upsert_post(self, p: Post) -> None:
        existing = self.db.execute("SELECT snapshots FROM posts WHERE id=?", (p.id,)).fetchone()
        snaps = list(p.snapshots)
        if existing:
            old = json.loads(existing["snapshots"] or "[]")
            seen = {tuple(s) for s in old}
            snaps = old + [list(s) for s in snaps if tuple(s) not in seen]
        self.db.execute(
            "INSERT INTO posts(id,platform,author,text,url,created_at,snapshots,own) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET snapshots=excluded.snapshots",
            (p.id, p.platform, p.author, p.text, p.url, p.created_at,
             json.dumps([list(s) for s in snaps]), int(p.own)),
        )
        self.db.commit()

    def get_post(self, post_id: str, brand: str) -> Post | None:
        r = self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not r:
            return None
        return Post(
            id=r["id"], brand=brand, platform=r["platform"], author=r["author"],
            text=r["text"], url=r["url"], created_at=r["created_at"],
            snapshots=[tuple(s) for s in json.loads(r["snapshots"] or "[]")],
            own=bool(r["own"]),
        )

    def list_posts(self, brand: str, own: bool | None = None) -> list[Post]:
        q = "SELECT id FROM posts"
        args: tuple = ()
        if own is not None:
            q += " WHERE own=?"
            args = (int(own),)
        rows = self.db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [self.get_post(r["id"], brand) for r in rows]

    # -- engagement history --------------------------------------------------
    def record_touch(self, author: str, platform: str, post_id: str, ts: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO touches(author,platform,touched_at,post_id) VALUES(?,?,?,?)",
            (author, platform, ts or int(time.time()), post_id),
        )
        self.db.commit()

    def has_engaged(self, post_id: str) -> bool:
        r = self.db.execute("SELECT 1 FROM touches WHERE post_id=?", (post_id,)).fetchone()
        return r is not None

    def last_touch(self, author: str) -> int | None:
        r = self.db.execute(
            "SELECT MAX(touched_at) AS t FROM touches WHERE author=?", (author,)
        ).fetchone()
        return r["t"] if r and r["t"] else None

    # -- opportunities -------------------------------------------------------
    def save_opportunity(self, o: Opportunity) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO opportunities(post_id,score,components,excluded,exclusion_reason,scored_at) "
            "VALUES(?,?,?,?,?,?)",
            (o.post_id, o.score, json.dumps(o.components), int(o.excluded),
             o.exclusion_reason, int(time.time())),
        )
        self.db.commit()

    def top_opportunities(self, brand: str, limit: int = 5) -> list[tuple[Opportunity, Post]]:
        rows = self.db.execute(
            "SELECT * FROM opportunities WHERE excluded=0 ORDER BY score DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            o = Opportunity(
                post_id=r["post_id"], brand=brand, score=r["score"],
                components=json.loads(r["components"]),
            )
            p = self.get_post(r["post_id"], brand)
            if p:
                out.append((o, p))
        return out

    # -- drafts / approvals ----------------------------------------------------
    def save_draft(self, d: Draft) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO drafts(id,kind,platform,text,post_id,material_id,"
            "funnel_class,status,gate_reasons,angle,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d.id, d.kind, d.platform, d.text, d.post_id, d.material_id,
             d.funnel_class, d.status, json.dumps(d.gate_reasons), d.angle, int(time.time())),
        )
        self.db.commit()

    def get_draft(self, draft_id: str, brand: str) -> Draft | None:
        r = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not r:
            return None
        return Draft(
            id=r["id"], brand=brand, kind=r["kind"], platform=r["platform"], text=r["text"],
            post_id=r["post_id"], material_id=r["material_id"], funnel_class=r["funnel_class"],
            status=r["status"], gate_reasons=json.loads(r["gate_reasons"]), angle=r["angle"],
        )

    def list_drafts(self, brand: str, status: str | None = None) -> list[Draft]:
        q, args = "SELECT id FROM drafts", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        rows = self.db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [self.get_draft(r["id"], brand) for r in rows]

    def set_draft_status(self, draft_id: str, status: str, reasons: list[str] | None = None) -> None:
        if reasons is None:
            self.db.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        else:
            self.db.execute(
                "UPDATE drafts SET status=?, gate_reasons=? WHERE id=?",
                (status, json.dumps(reasons), draft_id),
            )
        self.db.commit()

    def record_approval(self, draft_id: str, content_hash: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO approvals(draft_id,content_hash,approved_at) VALUES(?,?,?)",
            (draft_id, content_hash, int(time.time())),
        )
        self.db.commit()

    def get_approval_hash(self, draft_id: str) -> str | None:
        r = self.db.execute("SELECT content_hash FROM approvals WHERE draft_id=?", (draft_id,)).fetchone()
        return r["content_hash"] if r else None

    def revoke_approval(self, draft_id: str) -> None:
        self.db.execute("DELETE FROM approvals WHERE draft_id=?", (draft_id,))
        self.db.commit()

    def record_published(self, draft_id: str, platform: str, external_url: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO published(draft_id,platform,published_at,external_url) VALUES(?,?,?,?)",
            (draft_id, platform, int(time.time()), external_url),
        )
        self.db.commit()

    def published_count_since(self, platform: str, since_ts: int) -> int:
        r = self.db.execute(
            "SELECT COUNT(*) AS c FROM published WHERE platform=? AND published_at>=?",
            (platform, since_ts),
        ).fetchone()
        return r["c"]

    # -- source material -------------------------------------------------------
    def add_material(self, mid: str, title: str, text: str, kind: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO materials(id,title,text,kind,added_at) VALUES(?,?,?,?,?)",
            (mid, title, text, kind, int(time.time())),
        )
        self.db.commit()

    def get_material(self, mid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()

    def list_materials(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM materials ORDER BY added_at DESC").fetchall()

    def record_material_use(self, material_id: str, draft_id: str, platform: str) -> None:
        self.db.execute(
            "INSERT INTO material_uses(material_id,draft_id,platform,used_at) VALUES(?,?,?,?)",
            (material_id, draft_id, platform, int(time.time())),
        )
        self.db.commit()

    def material_used_on(self, material_id: str, platform: str, within_days: int) -> bool:
        cutoff = int(time.time()) - within_days * 86400
        r = self.db.execute(
            "SELECT 1 FROM material_uses WHERE material_id=? AND platform=? AND used_at>=?",
            (material_id, platform, cutoff),
        ).fetchone()
        return r is not None

    def published_since(self, since_ts: int) -> list[sqlite3.Row]:
        """Published rows joined with their draft text — feeds measure/weekly."""
        return self.db.execute(
            "SELECT p.draft_id, p.platform, p.published_at, p.external_url, "
            "d.text, d.kind, d.funnel_class, d.material_id "
            "FROM published p JOIN drafts d ON d.id=p.draft_id "
            "WHERE p.published_at>=? ORDER BY p.published_at DESC",
            (since_ts,),
        ).fetchall()

    # -- metrics (feedback loop) ------------------------------------------------
    def record_metric(self, post_id: str, metric: str, value: float) -> None:
        self.db.execute(
            "INSERT INTO metrics(post_id,metric,value,measured_at) VALUES(?,?,?,?)",
            (post_id, metric, value, int(time.time())),
        )
        self.db.commit()

    def set_metric(self, post_id: str, metric: str, value: float) -> None:
        """Replace-not-append: recomputed metrics (normalizations) must not
        accumulate stale rows that would skew history averages."""
        self.db.execute("DELETE FROM metrics WHERE post_id=? AND metric=?", (post_id, metric))
        self.record_metric(post_id, metric, value)

    def latest_metric(self, post_id: str, metric: str) -> float | None:
        r = self.db.execute(
            "SELECT value FROM metrics WHERE post_id=? AND metric=? "
            "ORDER BY measured_at DESC LIMIT 1", (post_id, metric),
        ).fetchone()
        return r["value"] if r else None

    def latest_metrics_by_post(self, metric: str) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT post_id, value FROM metrics WHERE metric=? "
            "ORDER BY measured_at ASC", (metric,),
        ).fetchall()
        return {r["post_id"]: r["value"] for r in rows}  # last write wins

    def author_history_score(self, author: str) -> float | None:
        """Mean normalized performance of our published posts that touched this author.

        Returns None when there is no history (scorer treats that as neutral).
        """
        rows = self.db.execute(
            "SELECT m.value FROM metrics m JOIN touches t ON m.post_id=t.post_id "
            "WHERE t.author=? AND m.metric='performance_norm'",
            (author,),
        ).fetchall()
        if not rows:
            return None
        vals = [r["value"] for r in rows]
        return max(0.0, min(1.0, sum(vals) / len(vals)))
