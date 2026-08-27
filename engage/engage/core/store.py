"""Per-brand SQLite store. One DB file per brand, bound at construction.

There is deliberately no cross-brand query surface: a Store instance knows one
database file and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import (
    Comment,
    ContentRecord,
    Draft,
    Experiment,
    LifecycleError,
    Opportunity,
    PerformanceRecord,
    Post,
    ReplyDraft,
)

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
CREATE TABLE IF NOT EXISTS content_records (
    id TEXT PRIMARY KEY, brand TEXT, platform TEXT, kind TEXT,
    draft_id TEXT DEFAULT '', brief TEXT DEFAULT '{}', pillar TEXT DEFAULT '',
    lifecycle_status TEXT, created_at INTEGER, updated_at INTEGER,
    scheduled_at INTEGER DEFAULT NULL, timezone TEXT DEFAULT 'America/New_York',
    publish_method TEXT DEFAULT '', sink_job_id TEXT DEFAULT '',
    published_at INTEGER DEFAULT NULL, verification_status TEXT DEFAULT '',
    verification_evidence TEXT DEFAULT '{}', published_url TEXT DEFAULT '',
    platform_post_id TEXT DEFAULT '', retry_count INTEGER DEFAULT 0,
    last_error TEXT DEFAULT ''
);
-- A draft may back at most one content record — closes the path where two
-- records could both claim the same draft_id and both end up "approved" off
-- a single approval. Empty string is excluded: every record starts with
-- draft_id='' before gates run, so many rows share that value legitimately.
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_records_draft_id
    ON content_records(draft_id) WHERE draft_id != '';
CREATE TABLE IF NOT EXISTS content_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content_record_id TEXT,
    event_type TEXT, detail TEXT DEFAULT '{}', actor TEXT DEFAULT 'system',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS performance_records (
    id TEXT PRIMARY KEY, brand TEXT, content_record_id TEXT, content_hash TEXT,
    account TEXT DEFAULT '', platform TEXT DEFAULT '', campaign TEXT DEFAULT NULL,
    pillar TEXT DEFAULT '', format TEXT DEFAULT '', hook_type TEXT DEFAULT '',
    cta TEXT DEFAULT '', creative_asset_id TEXT DEFAULT NULL, publish_time INTEGER DEFAULT NULL,
    metrics TEXT DEFAULT '{}', data_coverage TEXT DEFAULT 'none',
    attribution_status TEXT DEFAULT 'unattributed', source TEXT DEFAULT 'manual_import',
    data_collected_at INTEGER DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY, brand TEXT, business_objective TEXT, hypothesis TEXT,
    independent_variable TEXT, control TEXT, treatment TEXT, target_platform TEXT,
    target_account TEXT DEFAULT '', success_metric TEXT, decision_rule TEXT,
    constant_conditions TEXT DEFAULT '[]', sample_or_evaluation_threshold TEXT DEFAULT '',
    date_range_start INTEGER DEFAULT NULL, date_range_end INTEGER DEFAULT NULL,
    risks TEXT DEFAULT '[]', approval_required INTEGER DEFAULT 1, approved_by TEXT DEFAULT '',
    status TEXT DEFAULT 'proposed', outcome TEXT DEFAULT '{}', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY, brand TEXT, target TEXT, label TEXT, summary TEXT,
    evidence TEXT DEFAULT '{}', confidence TEXT DEFAULT '', cross_brand INTEGER DEFAULT 0,
    authorization TEXT DEFAULT '', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY, brand TEXT, platform TEXT, account TEXT, author TEXT, text TEXT,
    source_type TEXT, source_id TEXT, external_id TEXT DEFAULT '', content_hash TEXT DEFAULT '',
    text_hash TEXT, imported_at INTEGER, triage_class TEXT DEFAULT '',
    triage_confidence REAL DEFAULT 0, triage_reason TEXT DEFAULT ''
);
-- Duplicate-import guard: the same external comment (when the platform gives
-- us a real id) must not be imported twice as two different rows. Manual/
-- fixture rows without an external_id ('') are excluded — many legitimately
-- share that empty value.
CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_external
    ON comments(brand, platform, source_id, external_id) WHERE external_id != '';
CREATE TABLE IF NOT EXISTS reply_drafts (
    id TEXT PRIMARY KEY, brand TEXT, platform TEXT, account TEXT, comment_id TEXT,
    comment_text_hash TEXT, draft_text TEXT, draft_text_hash TEXT, angle TEXT DEFAULT '',
    brand_strategy_source TEXT DEFAULT '', review_status TEXT DEFAULT 'draft',
    gate_reasons TEXT DEFAULT '[]', generated_at INTEGER
);
-- Idempotency: the same comment (at the same text version) must not get two
-- reply drafts for the same angle from two separate generation calls.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_drafts_dedup
    ON reply_drafts(comment_id, comment_text_hash, angle);
-- Posts OBSERVED on a platform that this system did not publish. Deliberately
-- NOT content_records: such a post never held a brief, never passed a gate and
-- never had an approval row, so giving it a lifecycle_status would assert a
-- history that did not happen. Different entity, different table.
--
-- This exists because content_records answers "what did WE do", and every
-- duplicate check that consulted it alone was really answering that weaker
-- question — returning a confident "not published" for anything posted by
-- hand or by another tool.
--
-- content_record_id is '' when the post has no known origin here; it is set
-- only when reconciliation can attribute the post to a specific record.
CREATE TABLE IF NOT EXISTS external_posts (
    id TEXT PRIMARY KEY, brand TEXT, platform TEXT, channel_id TEXT DEFAULT '',
    platform_post_id TEXT, title TEXT DEFAULT '', url TEXT DEFAULT '',
    published_at INTEGER DEFAULT NULL, observed_at INTEGER,
    source TEXT DEFAULT '', content_record_id TEXT DEFAULT ''
);
-- Identity is the platform's own post id, scoped to brand+platform. The write
-- path uses ON CONFLICT(...) DO UPDATE, never INSERT OR REPLACE: a REPLACE
-- clause resolves a violation of this index by DELETING the prior row, which
-- is the silent loss this index exists to prevent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_posts_identity
    ON external_posts(brand, platform, platform_post_id);
"""


# ---------------------------------------------------------------------------
# Published-identifier invariant
# ---------------------------------------------------------------------------
# A confirmed published identifier (published_url / platform_post_id) may
# exist ONLY at these lifecycle states. See save_content_record().
PUBLISHED_ID_STATES = ("published", "verified", "failed")

# ---------------------------------------------------------------------------
# Reconciliation event vocabulary
# ---------------------------------------------------------------------------
AMBIGUOUS_EVENT = "upload_ambiguous"
RECONCILIATION_RESOLVED_EVENTS = (
    "reconciliation_confirmed_remote_post",
    "reconciliation_confirmed_no_remote_post",
)

RECONCILIATION_OPEN = "open_reconciliation"
RECONCILIATION_INCONSISTENT = "lifecycle_inconsistency"


@dataclass(frozen=True)
class ReconciliationState:
    """Whether a record has an unresolved remote-upload outcome, plus the
    diagnostics needed to act on it.

    `required` is decided ONLY by event ids. `lifecycle_status` and `category`
    are reported alongside and never gate the answer, so a record cannot leave
    reconciliation reporting by changing lifecycle state — only a later
    explicit resolution event can do that.

    NOTE ON 'publishing'. A record with an unresolved outcome sits at
    'publishing' because that is the existing technical parking state, kept
    for compatibility and duplicate protection. It does NOT mean ordinary
    publishing work is in progress, and nothing may describe it as healthy
    in-flight work."""

    record_id: str
    required: bool
    lifecycle_status: str
    category: str
    ambiguous_event_id: int | None
    resolution_event_id: int | None

    @property
    def is_urgent(self) -> bool:
        return self.category == RECONCILIATION_INCONSISTENT

    @property
    def is_resolvable(self) -> bool:
        return self.required and self.category == RECONCILIATION_OPEN


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

    # -- content records / event log (Phase 1 — drafting/originals.py) --------
    def save_content_record(self, r: ContentRecord) -> None:
        # Structural backstop: 'approved' may only ever be persisted once a
        # real approval row exists for the underlying draft — this holds
        # regardless of which code path set the field, not just for callers
        # that go through originals.approve_content_record().
        if r.lifecycle_status == "approved" and self.get_approval_hash(r.draft_id) is None:
            raise LifecycleError(
                f"refusing to persist content record {r.id} as 'approved' — no "
                f"approval record exists for its draft ({r.draft_id!r})"
            )

        # Second structural backstop. The first real upload produced a record
        # carrying a live published_url while its lifecycle still read
        # 'approved' — the caller set the fields without walking the legal
        # states. Ordering discipline alone did not prevent it, so the
        # combination is rejected at rest, whatever the caller does.
        #
        # 'failed' is permitted deliberately: published -> failed is legal and
        # means a POST-publication verification failure. Clearing the
        # identifier there would destroy the only pointer to a real public
        # object. 'failed' is unreachable from 'approved' or 'scheduled', and
        # 'publishing' + identifier is rejected by this very check, so a
        # URL-bearing 'failed' can only have arrived via 'published'.
        if r.lifecycle_status not in PUBLISHED_ID_STATES and (
            r.published_url or r.platform_post_id
        ):
            raise LifecycleError(
                f"refusing to persist content record {r.id} as {r.lifecycle_status!r} "
                "while carrying a published URL/post id — a confirmed published identifier "
                "may exist only at 'published', 'verified', or 'failed'; a URL-bearing "
                "'failed' record must represent a post-publication verification failure"
            )
        now = int(time.time())
        existing = self.db.execute(
            "SELECT created_at FROM content_records WHERE id=?", (r.id,)
        ).fetchone()
        # ON CONFLICT(id) only — NOT "INSERT OR REPLACE", which resolves a
        # UNIQUE-index violation (idx_content_records_draft_id) by silently
        # deleting the OTHER row and proceeding, rather than raising. Two
        # content records must never be able to share a draft_id; this way
        # SQLite raises IntegrityError instead of quietly destroying the
        # first record.
        self.db.execute(
            "INSERT INTO content_records(id,brand,platform,kind,draft_id,"
            "brief,pillar,lifecycle_status,created_at,updated_at,"
            "scheduled_at,timezone,publish_method,sink_job_id,published_at,"
            "verification_status,verification_evidence,published_url,"
            "platform_post_id,retry_count,last_error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET brand=excluded.brand, platform=excluded.platform, "
            "kind=excluded.kind, draft_id=excluded.draft_id, brief=excluded.brief, "
            "pillar=excluded.pillar, lifecycle_status=excluded.lifecycle_status, "
            "updated_at=excluded.updated_at, scheduled_at=excluded.scheduled_at, "
            "timezone=excluded.timezone, publish_method=excluded.publish_method, "
            "sink_job_id=excluded.sink_job_id, published_at=excluded.published_at, "
            "verification_status=excluded.verification_status, "
            "verification_evidence=excluded.verification_evidence, "
            "published_url=excluded.published_url, platform_post_id=excluded.platform_post_id, "
            "retry_count=excluded.retry_count, last_error=excluded.last_error",
            (r.id, r.brand, r.platform, r.kind, r.draft_id, json.dumps(r.brief),
             r.pillar, r.lifecycle_status,
             existing["created_at"] if existing else now, now,
             r.scheduled_at, r.timezone, r.publish_method, r.sink_job_id, r.published_at,
             r.verification_status, json.dumps(r.verification_evidence), r.published_url,
             r.platform_post_id, r.retry_count, r.last_error),
        )
        self.db.commit()

    def get_content_record(self, record_id: str) -> ContentRecord | None:
        row = self.db.execute(
            "SELECT * FROM content_records WHERE id=?", (record_id,)
        ).fetchone()
        if not row:
            return None
        return ContentRecord(
            id=row["id"], brand=row["brand"], platform=row["platform"], kind=row["kind"],
            draft_id=row["draft_id"], brief=json.loads(row["brief"] or "{}"),
            pillar=row["pillar"], lifecycle_status=row["lifecycle_status"],
            scheduled_at=row["scheduled_at"], timezone=row["timezone"] or "America/New_York",
            publish_method=row["publish_method"] or "", sink_job_id=row["sink_job_id"] or "",
            published_at=row["published_at"], verification_status=row["verification_status"] or "",
            verification_evidence=json.loads(row["verification_evidence"] or "{}"),
            published_url=row["published_url"] or "", platform_post_id=row["platform_post_id"] or "",
            retry_count=row["retry_count"] or 0, last_error=row["last_error"] or "",
        )

    def list_content_records(self, brand: str, lifecycle_status: str | None = None) -> list[ContentRecord]:
        q, args = "SELECT id FROM content_records WHERE brand=?", [brand]
        if lifecycle_status:
            q += " AND lifecycle_status=?"
            args.append(lifecycle_status)
        rows = self.db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [self.get_content_record(r["id"]) for r in rows]

    def _reconciliation_category(self, required: bool, lifecycle_status: str) -> str:
        if not required:
            return ""
        return (RECONCILIATION_OPEN if lifecycle_status == "publishing"
                else RECONCILIATION_INCONSISTENT)

    def record_requires_reconciliation(self, record_id: str) -> ReconciliationState:
        """Event-derived status: is there an unresolved remote-upload outcome?

        PREDICATE — authoritative, the only thing deciding `required`:

            max(upload_ambiguous.id) > max(resolution.id)
            with a missing resolution treated as -1

        ORDERING. By content_events.id, the AUTOINCREMENT rowid, strictly
        monotonic per insert. created_at is NEVER used for ordering or
        comparison: it is unix SECONDS and collides in practice — the live
        record aaafb1495c86 carries three timestamp collisions among eleven
        events, including a three-way one. created_at is display-only.

        MULTIPLE EVENTS. Only the highest id of each kind matters:
          * several ambiguous, one later resolution -> resolved
          * one ambiguous, several resolutions      -> resolved, latest wins
          * resolved, then a NEW ambiguous upload   -> required again

        LIFECYCLE IS NOT A FILTER. It is returned and classified. An
        unresolved outcome outside the 'publishing' parking state is
        RECONCILIATION_INCONSISTENT — urgent, never hidden, and not resolvable
        by the normal operations.

        Read-only. No network call, no credential access.
        """
        rec = self.db.execute(
            "SELECT lifecycle_status FROM content_records WHERE id=?", (record_id,)
        ).fetchone()
        lifecycle = rec["lifecycle_status"] if rec else ""
        ph = ",".join("?" * len(RECONCILIATION_RESOLVED_EVENTS))
        agg = self.db.execute(
            "SELECT "
            "  MAX(CASE WHEN event_type = ? THEN id END) AS ambiguous_id, "
            f" MAX(CASE WHEN event_type IN ({ph}) THEN id END) AS resolution_id "
            "FROM content_events WHERE content_record_id = ?",
            (AMBIGUOUS_EVENT, *RECONCILIATION_RESOLVED_EVENTS, record_id),
        ).fetchone()
        amb, res = agg["ambiguous_id"], agg["resolution_id"]
        required = amb is not None and amb > (res if res is not None else -1)
        return ReconciliationState(
            record_id=record_id, required=required, lifecycle_status=lifecycle,
            category=self._reconciliation_category(required, lifecycle),
            ambiguous_event_id=amb, resolution_event_id=res)

    def list_records_requiring_reconciliation(self, brand: str) -> list[ReconciliationState]:
        """Every record in this brand with an unresolved remote-upload
        outcome, ordered by when the outcome arose.

        NO lifecycle filter — a record that moved on while still carrying an
        unresolved outcome is exactly the case that must not be lost.
        content_events has no brand column, so content_records supplies it.
        Read-only."""
        ph = ",".join("?" * len(RECONCILIATION_RESOLVED_EVENTS))
        rows = self.db.execute(
            "SELECT e.content_record_id AS rid, r.lifecycle_status AS life, "
            "  MAX(CASE WHEN e.event_type = ? THEN e.id END) AS ambiguous_id, "
            f" MAX(CASE WHEN e.event_type IN ({ph}) THEN e.id END) AS resolution_id "
            "FROM content_events e "
            "JOIN content_records r ON r.id = e.content_record_id "
            "WHERE r.brand = ? "
            "GROUP BY e.content_record_id, r.lifecycle_status "
            "HAVING ambiguous_id IS NOT NULL "
            "   AND (resolution_id IS NULL OR resolution_id < ambiguous_id) "
            "ORDER BY ambiguous_id ASC",
            (AMBIGUOUS_EVENT, *RECONCILIATION_RESOLVED_EVENTS, brand),
        ).fetchall()
        return [
            ReconciliationState(
                record_id=r["rid"], required=True, lifecycle_status=r["life"] or "",
                category=self._reconciliation_category(True, r["life"] or ""),
                ambiguous_event_id=r["ambiguous_id"], resolution_event_id=r["resolution_id"])
            for r in rows]

    def foreign_brand_content_records(self, brand: str) -> list[str]:
        """Ids of content records in THIS brand's database file whose brand
        field is something else. One DB per brand is the isolation boundary,
        so any such row is contamination at rest by definition. Read-only.

        list_content_records() filters WHERE brand=?, so a contaminated row
        is invisible to it — which is safe, but also means a scan built on
        that method alone can never detect contamination. This is the query
        that actually can."""
        rows = self.db.execute(
            "SELECT id FROM content_records WHERE brand != ?", (brand,)
        ).fetchall()
        return [r["id"] for r in rows]

    def record_content_event(self, content_record_id: str, event_type: str,
                             detail: dict | None = None, actor: str = "system") -> None:
        self.db.execute(
            "INSERT INTO content_events(content_record_id,event_type,detail,actor,created_at) "
            "VALUES(?,?,?,?,?)",
            (content_record_id, event_type, json.dumps(detail or {}), actor, int(time.time())),
        )
        self.db.commit()

    def list_content_events(self, content_record_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT event_type, detail, actor, created_at FROM content_events "
            "WHERE content_record_id=? ORDER BY id ASC",
            (content_record_id,),
        ).fetchall()
        return [{"event_type": r["event_type"], "detail": json.loads(r["detail"] or "{}"),
                 "actor": r["actor"], "created_at": r["created_at"]} for r in rows]

    # -- externally-observed posts -------------------------------------------
    def record_external_post(self, *, brand: str, platform: str, platform_post_id: str,
                             channel_id: str = "", title: str = "", url: str = "",
                             published_at: int | None = None, source: str = "",
                             content_record_id: str = "") -> str:
        """Record one post seen on a platform that this system did not publish.

        Idempotent on (brand, platform, platform_post_id). Uses ON CONFLICT
        DO UPDATE rather than INSERT OR REPLACE so re-observing a post
        refreshes its mutable fields instead of deleting and re-inserting the
        row — a REPLACE would discard an attribution already recorded here.

        `observed_at` is deliberately NOT refreshed on conflict: it records
        when this system FIRST saw the post, which is the only thing it can
        honestly claim. `published_at` comes from the platform.
        """
        if not platform_post_id:
            raise ValueError("platform_post_id is required — an external post is identified "
                             "by the platform's own id, never by title or url")
        # The synthetic primary key must carry every column of the identity
        # index. Omitting brand made two brands observing the same platform
        # id collide on `id` — raising before ON CONFLICT could resolve it
        # against the intended (brand, platform, platform_post_id) key.
        row_id = f"ext_{brand}_{platform}_{platform_post_id}"
        observed_at = int(time.time())
        # A row with a NULL published_at is recorded but silently unsortable:
        # every query that ORDERs or filters on that column drops it without
        # error. That is the same "absence is not evidence" failure this table
        # was built to end, reappearing one layer down. Only the RSS path
        # supplies a real timestamp; the manual record path never does, so
        # fall back to observed_at instead of storing NULL. There is no
        # migration mechanism here, so this cannot be a NOT NULL column
        # constraint — the guard has to live on the write path.
        effective_published = published_at if published_at is not None else observed_at
        self.db.execute(
            "INSERT INTO external_posts(id,brand,platform,channel_id,platform_post_id,title,url,"
            "published_at,observed_at,source,content_record_id) "
            "VALUES(:id,:brand,:platform,:channel_id,:post_id,:title,:url,"
            ":published_at,:observed_at,:source,:record_id) "
            "ON CONFLICT(brand,platform,platform_post_id) DO UPDATE SET "
            "title=excluded.title, url=excluded.url, "
            # Only a caller-supplied timestamp may overwrite one already held.
            # The observed_at fallback must never downgrade a real platform
            # timestamp recorded by an earlier pass.
            "published_at=COALESCE(:raw_published, external_posts.published_at, "
            "excluded.published_at), "
            "channel_id=excluded.channel_id, source=excluded.source, "
            "content_record_id=CASE WHEN excluded.content_record_id != '' "
            "THEN excluded.content_record_id ELSE external_posts.content_record_id END",
            {"id": row_id, "brand": brand, "platform": platform,
             "channel_id": channel_id, "post_id": platform_post_id,
             "title": title, "url": url, "published_at": effective_published,
             "observed_at": observed_at, "source": source,
             "record_id": content_record_id, "raw_published": published_at},
        )
        self.db.commit()
        return row_id

    def list_external_posts(self, brand: str, platform: str | None = None) -> list[dict]:
        sql = "SELECT * FROM external_posts WHERE brand=?"
        args: list = [brand]
        if platform is not None:
            sql += " AND platform=?"
            args.append(platform)
        sql += " ORDER BY published_at DESC"
        return [dict(r) for r in self.db.execute(sql, tuple(args)).fetchall()]

    def get_external_post(self, brand: str, platform: str, platform_post_id: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM external_posts WHERE brand=? AND platform=? AND platform_post_id=?",
            (brand, platform, platform_post_id),
        ).fetchone()
        return dict(r) if r else None

    # -- performance ledger / experiments / recommendations (Phase 3) ---------
    def save_performance_record(self, r: PerformanceRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO performance_records(id,brand,content_record_id,content_hash,"
            "account,platform,campaign,pillar,format,hook_type,cta,creative_asset_id,publish_time,"
            "metrics,data_coverage,attribution_status,source,data_collected_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.id, r.brand, r.content_record_id, r.content_hash, r.account, r.platform, r.campaign,
             r.pillar, r.format, r.hook_type, r.cta, r.creative_asset_id, r.publish_time,
             json.dumps(r.metrics), r.data_coverage, r.attribution_status, r.source, r.data_collected_at),
        )
        self.db.commit()

    def get_performance_record(self, record_id: str) -> PerformanceRecord | None:
        row = self.db.execute(
            "SELECT * FROM performance_records WHERE id=?", (record_id,)
        ).fetchone()
        return self._row_to_performance_record(row) if row else None

    def list_performance_records(self, brand: str, content_record_id: str | None = None) -> list[PerformanceRecord]:
        q, args = "SELECT * FROM performance_records WHERE brand=?", [brand]
        if content_record_id:
            q += " AND content_record_id=?"
            args.append(content_record_id)
        rows = self.db.execute(q, args).fetchall()
        return [self._row_to_performance_record(r) for r in rows]

    @staticmethod
    def _row_to_performance_record(row: sqlite3.Row) -> PerformanceRecord:
        return PerformanceRecord(
            id=row["id"], brand=row["brand"], content_record_id=row["content_record_id"],
            content_hash=row["content_hash"], account=row["account"] or "", platform=row["platform"] or "",
            campaign=row["campaign"], pillar=row["pillar"] or "", format=row["format"] or "",
            hook_type=row["hook_type"] or "", cta=row["cta"] or "", creative_asset_id=row["creative_asset_id"],
            publish_time=row["publish_time"], metrics=json.loads(row["metrics"] or "{}"),
            data_coverage=row["data_coverage"] or "none", attribution_status=row["attribution_status"] or "unattributed",
            source=row["source"] or "manual_import", data_collected_at=row["data_collected_at"],
        )

    def save_experiment(self, e: Experiment) -> None:
        now = int(time.time())
        existing = self.db.execute("SELECT created_at FROM experiments WHERE id=?", (e.id,)).fetchone()
        self.db.execute(
            "INSERT OR REPLACE INTO experiments(id,brand,business_objective,hypothesis,"
            "independent_variable,control,treatment,target_platform,target_account,success_metric,"
            "decision_rule,constant_conditions,sample_or_evaluation_threshold,date_range_start,"
            "date_range_end,risks,approval_required,approved_by,status,outcome,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.id, e.brand, e.business_objective, e.hypothesis, e.independent_variable, e.control,
             e.treatment, e.target_platform, e.target_account, e.success_metric, e.decision_rule,
             json.dumps(e.constant_conditions), e.sample_or_evaluation_threshold, e.date_range_start,
             e.date_range_end, json.dumps(e.risks), int(e.approval_required), e.approved_by,
             e.status, json.dumps(e.outcome), e.created_at or (existing["created_at"] if existing else now)),
        )
        self.db.commit()

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        row = self.db.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        return self._row_to_experiment(row) if row else None

    def list_experiments(self, brand: str, status: str | None = None) -> list[Experiment]:
        q, args = "SELECT * FROM experiments WHERE brand=?", [brand]
        if status:
            q += " AND status=?"
            args.append(status)
        rows = self.db.execute(q, args).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    @staticmethod
    def _row_to_experiment(row: sqlite3.Row) -> Experiment:
        return Experiment(
            id=row["id"], brand=row["brand"], business_objective=row["business_objective"],
            hypothesis=row["hypothesis"], independent_variable=row["independent_variable"],
            control=row["control"], treatment=row["treatment"], target_platform=row["target_platform"],
            target_account=row["target_account"] or "", success_metric=row["success_metric"],
            decision_rule=row["decision_rule"], constant_conditions=json.loads(row["constant_conditions"] or "[]"),
            sample_or_evaluation_threshold=row["sample_or_evaluation_threshold"] or "",
            date_range_start=row["date_range_start"], date_range_end=row["date_range_end"],
            risks=json.loads(row["risks"] or "[]"), approval_required=bool(row["approval_required"]),
            approved_by=row["approved_by"] or "", status=row["status"], outcome=json.loads(row["outcome"] or "{}"),
            created_at=row["created_at"],
        )

    def save_recommendation(self, rec: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO recommendations(id,brand,target,label,summary,evidence,"
            "confidence,cross_brand,authorization,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["brand"], rec["target"], rec["label"], rec["summary"],
             json.dumps(rec["evidence"]), rec["confidence"], int(rec["cross_brand"]),
             rec["authorization"], rec["created_at"]),
        )
        self.db.commit()

    def list_recommendations(self, brand: str, target: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM recommendations WHERE brand=?", [brand]
        if target:
            q += " AND target=?"
            args.append(target)
        rows = self.db.execute(q, args).fetchall()
        return [{"id": r["id"], "brand": r["brand"], "target": r["target"], "label": r["label"],
                 "summary": r["summary"], "evidence": json.loads(r["evidence"] or "{}"),
                 "confidence": r["confidence"], "cross_brand": bool(r["cross_brand"]),
                 "authorization": r["authorization"], "created_at": r["created_at"]} for r in rows]

    # -- comments / reply drafts (Phase 5 — engagement/) -----------------------
    def get_comment_by_external_id(self, brand: str, platform: str, source_id: str,
                                   external_id: str) -> Comment | None:
        if not external_id:
            return None
        row = self.db.execute(
            "SELECT * FROM comments WHERE brand=? AND platform=? AND source_id=? AND external_id=?",
            (brand, platform, source_id, external_id),
        ).fetchone()
        return self._row_to_comment(row) if row else None

    def get_comment(self, comment_id: str) -> Comment | None:
        row = self.db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        return self._row_to_comment(row) if row else None

    def save_comment(self, c: Comment) -> None:
        # ON CONFLICT(id) only — see save_content_record()'s comment on why
        # INSERT OR REPLACE is unsafe with a second UNIQUE index present
        # (idx_comments_external): REPLACE resolves that index's conflicts by
        # silently deleting the other row instead of raising.
        self.db.execute(
            "INSERT INTO comments(id,brand,platform,account,author,text,source_type,source_id,"
            "external_id,content_hash,text_hash,imported_at,triage_class,triage_confidence,triage_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET brand=excluded.brand, platform=excluded.platform, "
            "account=excluded.account, author=excluded.author, text=excluded.text, "
            "source_type=excluded.source_type, source_id=excluded.source_id, "
            "external_id=excluded.external_id, content_hash=excluded.content_hash, "
            "text_hash=excluded.text_hash, imported_at=excluded.imported_at, "
            "triage_class=excluded.triage_class, triage_confidence=excluded.triage_confidence, "
            "triage_reason=excluded.triage_reason",
            (c.id, c.brand, c.platform, c.account, c.author, c.text, c.source_type, c.source_id,
             c.external_id, c.content_hash, c.text_hash, c.imported_at, c.triage_class,
             c.triage_confidence, c.triage_reason),
        )
        self.db.commit()

    def list_comments(self, brand: str, source_id: str | None = None) -> list[Comment]:
        q, args = "SELECT id FROM comments WHERE brand=?", [brand]
        if source_id:
            q += " AND source_id=?"
            args.append(source_id)
        rows = self.db.execute(q + " ORDER BY imported_at DESC", args).fetchall()
        return [self.get_comment(r["id"]) for r in rows]

    @staticmethod
    def _row_to_comment(row: sqlite3.Row) -> Comment:
        return Comment(
            id=row["id"], brand=row["brand"], platform=row["platform"], account=row["account"],
            author=row["author"], text=row["text"], source_type=row["source_type"],
            source_id=row["source_id"], external_id=row["external_id"] or "",
            content_hash=row["content_hash"] or "", text_hash=row["text_hash"] or "",
            imported_at=row["imported_at"], triage_class=row["triage_class"] or "",
            triage_confidence=row["triage_confidence"] or 0.0, triage_reason=row["triage_reason"] or "",
        )

    def get_reply_draft_by_dedup_key(self, comment_id: str, comment_text_hash: str,
                                     angle: str) -> ReplyDraft | None:
        row = self.db.execute(
            "SELECT * FROM reply_drafts WHERE comment_id=? AND comment_text_hash=? AND angle=?",
            (comment_id, comment_text_hash, angle),
        ).fetchone()
        return self._row_to_reply_draft(row) if row else None

    def get_reply_draft(self, draft_id: str) -> ReplyDraft | None:
        row = self.db.execute("SELECT * FROM reply_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._row_to_reply_draft(row) if row else None

    def save_reply_draft(self, d: ReplyDraft) -> None:
        self.db.execute(
            "INSERT INTO reply_drafts(id,brand,platform,account,comment_id,comment_text_hash,"
            "draft_text,draft_text_hash,angle,brand_strategy_source,review_status,gate_reasons,"
            "generated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET review_status=excluded.review_status, "
            "gate_reasons=excluded.gate_reasons, draft_text=excluded.draft_text, "
            "draft_text_hash=excluded.draft_text_hash",
            (d.id, d.brand, d.platform, d.account, d.comment_id, d.comment_text_hash, d.draft_text,
             d.draft_text_hash, d.angle, d.brand_strategy_source, d.review_status,
             json.dumps(d.gate_reasons), d.generated_at),
        )
        self.db.commit()

    def list_reply_drafts(self, brand: str, comment_id: str | None = None,
                          review_status: str | None = None) -> list[ReplyDraft]:
        q, args = "SELECT id FROM reply_drafts WHERE brand=?", [brand]
        if comment_id:
            q += " AND comment_id=?"
            args.append(comment_id)
        if review_status:
            q += " AND review_status=?"
            args.append(review_status)
        rows = self.db.execute(q + " ORDER BY generated_at DESC", args).fetchall()
        return [self.get_reply_draft(r["id"]) for r in rows]

    @staticmethod
    def _row_to_reply_draft(row: sqlite3.Row) -> ReplyDraft:
        return ReplyDraft(
            id=row["id"], brand=row["brand"], platform=row["platform"], account=row["account"],
            comment_id=row["comment_id"], comment_text_hash=row["comment_text_hash"],
            draft_text=row["draft_text"], draft_text_hash=row["draft_text_hash"] or "",
            angle=row["angle"] or "", brand_strategy_source=row["brand_strategy_source"] or "",
            review_status=row["review_status"] or "draft",
            gate_reasons=json.loads(row["gate_reasons"] or "[]"), generated_at=row["generated_at"],
        )

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
