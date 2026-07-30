#!/usr/bin/env python3
"""
memory_driver.py — SQLite backend for Copilot long-term memory.
Reads a JSON operation payload from stdin, dispatches, prints result as JSON to stdout.

Usage:
  echo '{"op": "add_topic", "slug": "gcc-gaps", "title": "GCC Product Gaps", "category": "technical"}' | python memory_driver.py
  echo '{"op": "export_context"}' | python memory_driver.py

All string values go through json.loads — no SQL-injection risk, no PowerShell quoting issues.
"""

import json
import sys
import sqlite3
import os
import io
import datetime

# Ensure UTF-8 stdin regardless of Windows console codepage.
# Deliberately NOT executed at import time: rewrapping sys.stdin on import
# breaks any test runner that has already closed or replaced it
# (ValueError: I/O operation on closed file under `unittest discover`).
def _ensure_utf8_stdin():
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "buffer"):
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    except (ValueError, AttributeError):
        pass

DEFAULT_DB = os.path.join(os.environ["USERPROFILE"], ".copilot", "memory.db")
DEFAULT_CTX = os.path.join(os.environ["USERPROFILE"], ".copilot", "memory-context.md")


def _db_path(payload):
    if payload and payload.get("db_path"):
        return payload["db_path"]
    return DEFAULT_DB


def _ctx_path(payload):
    if payload and payload.get("context_path"):
        return payload["context_path"]
    return DEFAULT_CTX


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ── operations ────────────────────────────────────────────────────────

def add_topic(p):
    slug = p["slug"]
    title = p.get("title", slug)
    category = p.get("category", "research")
    desc = p.get("description", "")
    status = p.get("status", "active")
    now = _now()
    with get_conn(_db_path(p)) as conn:
        conn.execute(
            """
            INSERT INTO topics(slug, title, category, description, status, last_accessed_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                title = CASE WHEN excluded.title != slug THEN excluded.title ELSE topics.title END,
                description = COALESCE(NULLIF(excluded.description,''), topics.description),
                status = excluded.status,
                last_accessed_at = excluded.last_accessed_at
            """,
            (slug, title, category, desc, status, now),
        )
        row = conn.execute("SELECT id FROM topics WHERE slug=?", (slug,)).fetchone()
        return {"topic_id": row[0]}


def get_topic(p):
    slug = p["slug"]
    with get_conn(_db_path(p)) as conn:
        row = conn.execute(
            "SELECT id, slug, title, category, status, last_accessed_at FROM topics WHERE slug=?",
            (slug,),
        ).fetchone()
        if row:
            return dict(row)
        return {"error": f"topic not found: {slug}"}


def add_fact(p):
    topic_id = p.get("topic_id")
    content = p["content"]
    fact_type = p.get("fact_type", "insight")
    confidence = p.get("confidence", 3)
    importance = p.get("importance", 3)
    source = p.get("source", "")
    session_id = p.get("session_id", "")
    now = _now()
    with get_conn(_db_path(p)) as conn:
        # Accept a slug directly so callers (and batch steps) do not need a
        # separate round trip to resolve the topic id.
        if topic_id is None and p.get("topic_slug"):
            row = conn.execute(
                "SELECT id FROM topics WHERE slug=?", (p["topic_slug"],)
            ).fetchone()
            if row:
                topic_id = row[0]
        # SQLite UNIQUE(topic_id, content) treats NULL != NULL, so global facts (topic_id=NULL)
        # are NOT caught by the constraint. Always pre-check with IS for NULL-safe comparison.
        existing = conn.execute(
            "SELECT id FROM facts WHERE topic_id IS ? AND content=?", (topic_id, content)
        ).fetchone()
        if existing:
            # Re-asserting a fact is itself a relevance signal — refresh recency.
            conn.execute(
                "UPDATE facts SET last_accessed_at=? WHERE id=?", (now, existing[0])
            )
            return {"fact_id": existing[0], "inserted": False}
        cur = conn.execute(
            """
            INSERT INTO facts
              (topic_id, content, fact_type, confidence, importance, source, session_id,
               last_accessed_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (topic_id, content, fact_type, confidence, importance, source, session_id, now),
        )
        # A fact inserted with last_accessed_at = NULL sorts LAST under
        # `ORDER BY last_accessed_at DESC`, so it never reaches export_context.
        # Setting it on insert is what makes a new fact visible immediately.
        return {"fact_id": cur.lastrowid, "inserted": True}


def add_entity(p):
    name = p["name"]
    entity_type = p["entity_type"]
    is_self = int(p.get("is_self", 0))
    attributes = p.get("attributes", "")
    notes = p.get("notes", "")
    with get_conn(_db_path(p)) as conn:
        conn.execute(
            """
            INSERT INTO entities(name, entity_type, is_self, attributes, notes)
            VALUES(?,?,?,?,?)
            ON CONFLICT(name, entity_type) DO UPDATE SET
                attributes = COALESCE(NULLIF(excluded.attributes,''), entities.attributes),
                notes      = COALESCE(NULLIF(excluded.notes,''), entities.notes),
                is_self    = MAX(entities.is_self, excluded.is_self)
            """,
            (name, entity_type, is_self, attributes, notes),
        )
        row = conn.execute(
            "SELECT id FROM entities WHERE name=? AND entity_type=?", (name, entity_type)
        ).fetchone()
        return {"entity_id": row[0]}


def add_snapshot(p):
    topic_id = p["topic_id"]
    title = p["title"]
    summary = p.get("summary", "")
    findings = p.get("findings", "")
    decisions = p.get("decisions", "")
    open_questions = p.get("open_questions", "")
    next_steps = p.get("next_steps", "")
    source_session = p.get("source_session", "")
    with get_conn(_db_path(p)) as conn:
        # seq_number is per-topic, not global
        cur_seq = conn.execute(
            "SELECT COALESCE(MAX(seq_number), 0) + 1 FROM snapshots WHERE topic_id=?",
            (topic_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO snapshots
              (topic_id, seq_number, title, summary, findings, decisions,
               open_questions, next_steps, source_session)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (topic_id, cur_seq, title, summary, findings, decisions,
             open_questions, next_steps, source_session),
        )
        return {"snapshot_id": cur.lastrowid, "seq_number": cur_seq}


def _md_inline(text):
    """Flatten stored text into one safe markdown line.

    Fact bodies are free-form and frequently contain newlines and markdown of
    their own. Emitted verbatim, a body containing a line like '## Pre-flight
    check' renders as a real document heading and corrupts the structure of
    memory-context.md. Collapsing all whitespace keeps every value inside its
    list item.
    """
    if not text:
        return ""
    return " ".join(str(text).split())


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# FTS5 reserved characters. A bare token containing any of these is parsed as
# syntax rather than text, e.g. "remember-this" becomes column:this.
_FTS_SPECIAL = set('-:^*()"')


def _fts_quote(token):
    """Wrap a token as an FTS5 string literal, escaping embedded quotes."""
    return '"' + token.replace('"', '""') + '"'


def _fts_sanitize(query):
    """Quote every whitespace-separated token that contains an FTS5 operator.

    Bare boolean keywords (AND / OR / NOT / NEAR) are preserved so a sanitized
    retry does not silently change the caller's intent.
    """
    booleans = {"AND", "OR", "NOT", "NEAR"}
    out = []
    for token in query.split():
        if token in booleans:
            out.append(token)
        elif any(ch in _FTS_SPECIAL for ch in token):
            out.append(_fts_quote(token))
        else:
            out.append(token)
    return " ".join(out)


def touch_fact(p):
    fact_id = p["fact_id"]
    now = _now()
    with get_conn(_db_path(p)) as conn:
        conn.execute("UPDATE facts SET last_accessed_at=? WHERE id=?", (now, fact_id))
    return {"updated": True}


def touch_topic(p):
    """Update last_accessed_at by topic_id or slug."""
    now = _now()
    with get_conn(_db_path(p)) as conn:
        if "topic_id" in p:
            conn.execute("UPDATE topics SET last_accessed_at=? WHERE id=?", (now, p["topic_id"]))
        elif "slug" in p:
            conn.execute("UPDATE topics SET last_accessed_at=? WHERE slug=?", (now, p["slug"]))
        else:
            return {"error": "provide topic_id or slug"}
    return {"updated": True}


_RESOLVE_SQL = {
    "facts": "SELECT id, content AS text, fact_type AS subtype FROM facts WHERE id IN (%s)",
    "entities": "SELECT id, name || ' ' || COALESCE(notes,'') AS text, entity_type AS subtype "
                "FROM entities WHERE id IN (%s)",
    "topics": "SELECT id, slug || ' ' || title || ' ' || COALESCE(description,'') AS text, "
              "category AS subtype FROM topics WHERE id IN (%s)",
    "snapshots": "SELECT id, COALESCE(summary,'') || ' ' || COALESCE(findings,'') AS text, "
                 "NULL AS subtype FROM snapshots WHERE id IN (%s)",
}


def _run_match(conn, query, limit):
    """Bounded FTS lookup, then resolve only the rows we actually matched.

    The previous implementation joined memory_fts against the fts_resolved view,
    a 4-way UNION ALL over every row in the database, so a 10-row search grew
    linearly with total size. Selecting hit ids first keeps resolution O(limit).
    """
    hits = conn.execute(
        """
        SELECT row_id, table_name, bm25(memory_fts) AS rank
        FROM memory_fts
        WHERE memory_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    if not hits:
        return []

    by_src = {}
    for h in hits:
        by_src.setdefault(h["table_name"], []).append(h["row_id"])

    resolved = {}
    for src, ids in by_src.items():
        sql = _RESOLVE_SQL.get(src)
        if not sql:
            continue
        placeholders = ",".join("?" * len(ids))
        for row in conn.execute(sql % placeholders, ids):
            resolved[(src, row["id"])] = (row["text"], row["subtype"])

    out = []
    for h in hits:
        key = (h["table_name"], h["row_id"])
        if key not in resolved:
            continue          # source row deleted but FTS entry lingered
        text, subtype = resolved[key]
        out.append({
            "src": h["table_name"],
            "id": h["row_id"],
            "text": text,
            "content": text,   # alias: .content and .text both work
            "subtype": subtype,
            "rank": h["rank"],
        })
    return out


def search_memory(p):
    """Full-text search across all memory tables.

    FTS5 treats '-', ':', '^', '*', '(' and ')' as query operators, so a plain
    term like "remember-this" or "cx-followup" raises
    OperationalError: no such column: this.

    Strategy: run the caller's query verbatim first so intentional FTS5 syntax
    (OR, NOT, "phrase", prefix*) keeps working. Only if SQLite rejects it do we
    auto-quote each bare token and retry. Set {"raw": true} to disable the retry.

    Each row exposes the body under BOTH `text` (historical) and `content`
    (ergonomic alias) so callers can use either without hitting a null.
    """
    query = p["query"]
    limit = p.get("limit", 10)
    raw_only = bool(p.get("raw", False))

    with get_conn(_db_path(p)) as conn:
        sanitized = False
        try:
            rows = _run_match(conn, query, limit)
        except sqlite3.OperationalError:
            if raw_only:
                raise
            safe = _fts_sanitize(query)
            if safe == query:
                raise
            rows = _run_match(conn, safe, limit)
            sanitized = True

        for r in rows:
            r["sanitized"] = sanitized
        return rows


def set_identity(p):
    """Set (or correct) the user's own entity — the source of the Identity block.

    There is no `identity` table: the Identity section of memory-context.md is
    rendered from the entities row where is_self = 1. This op makes that
    discoverable and guarantees exactly one self row.
    """
    name = p["name"]
    entity_type = p.get("entity_type", "person")
    attributes = p.get("attributes", "")
    notes = p.get("notes", "")

    with get_conn(_db_path(p)) as conn:
        conn.execute(
            """
            INSERT INTO entities(name, entity_type, is_self, attributes, notes)
            VALUES(?,?,1,?,?)
            ON CONFLICT(name, entity_type) DO UPDATE SET
                attributes = COALESCE(NULLIF(excluded.attributes,''), entities.attributes),
                notes      = COALESCE(NULLIF(excluded.notes,''), entities.notes),
                is_self    = 1
            """,
            (name, entity_type, attributes, notes),
        )
        row = conn.execute(
            "SELECT id FROM entities WHERE name=? AND entity_type=?", (name, entity_type)
        ).fetchone()
        # Exactly one self row: demote any other claimant.
        conn.execute("UPDATE entities SET is_self=0 WHERE id != ? AND is_self=1", (row[0],))
        return {"entity_id": row[0], "is_self": True}


SCHEMA_VERSION = 3


def optimize_db(p=None):
    """Apply the v3 performance schema to an existing database.

    Recreates the update triggers with WHEN guards and adds the covering
    indexes. Safe and idempotent.
    """
    with get_conn(_db_path(p or {})) as conn:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS facts_updated;
            DROP TRIGGER IF EXISTS fts_facts_update;
            DROP TRIGGER IF EXISTS fts_topics_update;

            CREATE TRIGGER facts_updated AFTER UPDATE ON facts
            WHEN OLD.content IS NOT NEW.content
              OR OLD.importance IS NOT NEW.importance
              OR OLD.status IS NOT NEW.status
              OR OLD.fact_type IS NOT NEW.fact_type
            BEGIN
              UPDATE facts SET updated_at = datetime('now') WHERE id = NEW.id;
            END;

            CREATE TRIGGER fts_facts_update AFTER UPDATE ON facts
            WHEN OLD.content IS NOT NEW.content
            BEGIN
              DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'facts';
              INSERT INTO memory_fts(content, row_id, table_name)
              VALUES (NEW.content, NEW.id, 'facts');
            END;

            CREATE TRIGGER fts_topics_update AFTER UPDATE ON topics
            WHEN OLD.slug IS NOT NEW.slug
              OR OLD.title IS NOT NEW.title
              OR OLD.description IS NOT NEW.description
            BEGIN
              DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'topics';
              INSERT INTO memory_fts(content, row_id, table_name)
              VALUES (NEW.slug || ' ' || NEW.title || ' ' || COALESCE(NEW.description, ''),
                      NEW.id, 'topics');
            END;

            CREATE INDEX IF NOT EXISTS idx_facts_topic_rank
              ON facts(topic_id, status, importance DESC, last_accessed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_facts_status_rank
              ON facts(status, importance DESC, last_accessed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_facts_pending
              ON facts(status, fact_type, importance DESC);
            CREATE INDEX IF NOT EXISTS idx_entities_recent
              ON entities(is_self, updated_at DESC);
            """
        )
        conn.execute("ANALYZE")
    return {"optimized": True, "schema_version": SCHEMA_VERSION}


def reindex_fts(p=None):
    """Rebuild memory_fts from the source tables, upgrading the topic triggers/view.

    v1 indexed topics as `title || description`, so searching by the slug you
    actually use everywhere (e.g. 'fy27-comp-plan') returned nothing. v2 adds
    the slug. Existing databases keep the old CREATE TRIGGER IF NOT EXISTS
    definitions, so they must be dropped and recreated explicitly.
    """
    with get_conn(_db_path(p or {})) as conn:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS fts_topics_insert;
            DROP TRIGGER IF EXISTS fts_topics_update;
            DROP VIEW    IF EXISTS fts_resolved;

            CREATE TRIGGER fts_topics_insert AFTER INSERT ON topics
            BEGIN
              INSERT INTO memory_fts(content, row_id, table_name)
              VALUES (NEW.slug || ' ' || NEW.title || ' ' || COALESCE(NEW.description, ''),
                      NEW.id, 'topics');
            END;

            CREATE TRIGGER fts_topics_update AFTER UPDATE ON topics
            BEGIN
              DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'topics';
              INSERT INTO memory_fts(content, row_id, table_name)
              VALUES (NEW.slug || ' ' || NEW.title || ' ' || COALESCE(NEW.description, ''),
                      NEW.id, 'topics');
            END;

            CREATE VIEW fts_resolved AS
              SELECT 'facts'     AS src, id, content AS text, fact_type AS subtype FROM facts
              UNION ALL
              SELECT 'entities'  AS src, id, name || ' ' || COALESCE(notes,'') AS text,
                     entity_type AS subtype FROM entities
              UNION ALL
              SELECT 'topics'    AS src, id,
                     slug || ' ' || title || ' ' || COALESCE(description,'') AS text,
                     category AS subtype FROM topics
              UNION ALL
              SELECT 'snapshots' AS src, id,
                     COALESCE(summary,'') || ' ' || COALESCE(findings,'') AS text,
                     NULL AS subtype FROM snapshots;
            """
        )
        # Rebuild the index content itself.
        conn.execute("DELETE FROM memory_fts")
        conn.execute(
            "INSERT INTO memory_fts(content, row_id, table_name) "
            "SELECT content, id, 'facts' FROM facts"
        )
        conn.execute(
            "INSERT INTO memory_fts(content, row_id, table_name) "
            "SELECT name || ' ' || COALESCE(notes,''), id, 'entities' FROM entities"
        )
        conn.execute(
            "INSERT INTO memory_fts(content, row_id, table_name) "
            "SELECT slug || ' ' || title || ' ' || COALESCE(description,''), id, 'topics' FROM topics"
        )
        conn.execute(
            "INSERT INTO memory_fts(content, row_id, table_name) "
            "SELECT COALESCE(summary,'') || ' ' || COALESCE(findings,''), id, 'snapshots' FROM snapshots"
        )
        total = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
    return {"fts_rows": total, "schema_version": SCHEMA_VERSION}


def migrate(p=None):
    """Idempotent upgrade + data repair for databases written by older versions.

    1. Backfills facts/topics whose last_accessed_at is NULL. Those rows sort
       LAST under `ORDER BY last_accessed_at DESC`, so any fact added before the
       add_fact fix was invisible to export_context until someone ran touch_fact
       by hand.
    2. Upgrades the FTS index to v2 (topic slug searchable) when needed.
    """
    p = p or {}
    with get_conn(_db_path(p)) as conn:
        facts = conn.execute(
            "UPDATE facts SET last_accessed_at = COALESCE(created_at, ?) "
            "WHERE last_accessed_at IS NULL",
            (_now(),),
        ).rowcount
        topics = conn.execute(
            "UPDATE topics SET last_accessed_at = COALESCE(created_at, ?) "
            "WHERE last_accessed_at IS NULL",
            (_now(),),
        ).rowcount
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        version = row[0] if row else 1

    reindexed = None
    if version < 2 or p.get("force_reindex"):
        reindexed = reindex_fts(p)

    optimized = None
    if version < 3 or p.get("force_optimize"):
        optimized = optimize_db(p)

    with get_conn(_db_path(p)) as conn:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    return {
        "facts_backfilled": facts,
        "topics_backfilled": topics,
        "schema_version": SCHEMA_VERSION,
        "from_version": version,
        "reindexed": reindexed,
        "optimized": optimized,
    }



def export_context(p=None):
    """Render memory-context.md — the bridge every agent reads at session start.

    Scalability model. The previous implementation took a single global
    `importance >= 4 ORDER BY importance DESC LIMIT 20` slice, which had two
    failure modes as the database grew:
      1. no per-topic fairness — one hot topic could consume all 20 slots;
      2. importance-4 facts became permanently unreachable once 20 importance-5
         facts existed, so they were written but never surfaced.

    Now each active topic gets its own quota, with a small reserved pool for
    critical (importance-5) cross-topic facts, and every limit is overridable
    via the payload so the file can scale with the database.
    """
    p = p or {}
    topic_limit    = p.get("topic_limit", 5)
    per_topic      = p.get("per_topic_facts", 6)
    max_facts      = p.get("max_facts", 40)
    min_importance = p.get("min_importance", 4)
    entity_limit   = p.get("entity_limit", 15)
    pending_limit  = p.get("pending_limit", 10)

    db_path = _db_path(p)
    ctx_path = _ctx_path(p)

    with get_conn(db_path) as conn:
        self_entity = conn.execute(
            "SELECT name, entity_type, attributes, notes FROM entities WHERE is_self=1 LIMIT 1"
        ).fetchone()

        topics = conn.execute(
            """
            SELECT id, slug, title, category, description, last_accessed_at
            FROM topics WHERE status='active'
            ORDER BY last_accessed_at DESC, id DESC
            LIMIT ?
            """,
            (topic_limit,),
        ).fetchall()

        # Per-topic quota — guarantees every loaded topic is represented.
        facts = []
        seen = set()
        for t in topics:
            rows = conn.execute(
                """
                SELECT f.id, f.content, f.fact_type, f.importance, f.confidence,
                       f.source, ? AS topic_slug
                FROM facts f
                WHERE f.topic_id = ? AND f.status='active' AND f.importance >= ?
                ORDER BY f.importance DESC, f.last_accessed_at DESC, f.id DESC
                LIMIT ?
                """,
                (t["slug"], t["id"], min_importance, per_topic),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    facts.append(dict(r))

        # Reserved pool: importance-5 facts from anywhere else (incl. topic_id NULL).
        # Without this, a critical global fact outside the top-N topics is lost.
        remaining = max(0, max_facts - len(facts))
        if remaining:
            rows = conn.execute(
                """
                SELECT f.id, f.content, f.fact_type, f.importance, f.confidence,
                       f.source, COALESCE(t.slug, 'general') AS topic_slug
                FROM facts f
                LEFT JOIN topics t ON t.id = f.topic_id
                WHERE f.status='active' AND f.importance >= 5
                ORDER BY f.last_accessed_at DESC, f.id DESC
                LIMIT ?
                """,
                (remaining + len(seen),),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen and len(facts) < max_facts:
                    seen.add(r["id"])
                    facts.append(dict(r))

        facts = facts[:max_facts]

        # Latest snapshot per loaded topic — the "where I left off" signal.
        snapshots = []
        for t in topics:
            row = conn.execute(
                """
                SELECT seq_number, title, next_steps, open_questions
                FROM snapshots WHERE topic_id=?
                ORDER BY seq_number DESC LIMIT 1
                """,
                (t["id"],),
            ).fetchone()
            if row:
                d = dict(row)
                d["topic_slug"] = t["slug"]
                snapshots.append(d)

        # Open work items, surfaced regardless of topic recency.
        pending = conn.execute(
            """
            SELECT f.content, f.fact_type, COALESCE(t.slug,'general') AS topic_slug
            FROM facts f
            LEFT JOIN topics t ON t.id = f.topic_id
            WHERE f.status='active' AND f.fact_type IN ('todo','action','question')
            ORDER BY f.importance DESC, f.last_accessed_at DESC
            LIMIT ?
            """,
            (pending_limit,),
        ).fetchall()

        entities = conn.execute(
            """
            SELECT name, entity_type, notes FROM entities
            WHERE is_self=0
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (entity_limit,),
        ).fetchall()

        now = _now()
        # Batched: one statement each instead of N. Combined with the WHEN-guarded
        # FTS triggers this keeps session start O(result size), not O(database).
        if topics:
            ids = [t["id"] for t in topics]
            conn.execute(
                "UPDATE topics SET last_accessed_at=? WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                [now] + ids,
            )
        if facts:
            ids = [f["id"] for f in facts]
            conn.execute(
                "UPDATE facts SET last_accessed_at=? WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                [now] + ids,
            )

    # ── build markdown ────────────────────────────────────────────────
    ts = _now()
    lines = [
        "# Copilot Long-Term Memory Context",
        f"<!-- Generated: {ts} -->",
        "",
        "_Read this at session start to restore persistent working memory._",
        "",
    ]

    if self_entity:
        lines += ["## 🧠 Identity", ""]
        lines.append(f"- **Name**: {_md_inline(self_entity['name'])}")
        if self_entity["attributes"]:
            lines.append(f"- **Attributes**: {_md_inline(self_entity['attributes'])}")
        if self_entity["notes"]:
            lines.append(f"- **Notes**: {_md_inline(self_entity['notes'])}")
        lines.append("")

    if topics:
        lines += ["## 📂 Active Topics (recently accessed)", ""]
        for t in topics:
            lines.append(f"### {_md_inline(t['title'])}")
            lines.append(
                f"- **Slug**: `{t['slug']}` | **Category**: {t['category']} "
                f"| **Last accessed**: {t['last_accessed_at'] or 'never'}"
            )
            if t["description"]:
                lines.append(f"- {_md_inline(t['description'])}")
            lines.append("")

    if facts:
        lines += [f"## ⭐ High-Priority Facts (importance ≥ {min_importance})", ""]
        by_topic = {}
        for f in facts:
            by_topic.setdefault(f["topic_slug"] or "general", []).append(f)
        for slug, topic_facts in by_topic.items():
            lines.append(f"### [{slug}]")
            for f in topic_facts:
                lines.append(
                    f"- {'⭐' * f['importance']} **[{f['fact_type']}]** {_md_inline(f['content'])}"
                )
                if f["source"]:
                    lines.append(f"  _Source: {_md_inline(f['source'])}_")
            lines.append("")

    if snapshots:
        lines += ["## 📌 Where I Left Off (latest snapshot per topic)", ""]
        for s in snapshots:
            lines.append(
                f"- **[{s['topic_slug']}]** {_md_inline(s['title'])} _(snapshot #{s['seq_number']})_"
            )
            if s["next_steps"]:
                lines.append(f"  - Next: {_md_inline(s['next_steps'])}")
            if s["open_questions"]:
                lines.append(f"  - Open: {_md_inline(s['open_questions'])}")
        lines.append("")

    if pending:
        lines += ["## ✅ Pending Work", ""]
        for w in pending:
            lines.append(
                f"- [{w['fact_type']}] _[{w['topic_slug']}]_ {_md_inline(w['content'])}"
            )
        lines.append("")

    if entities:
        lines += ["## 👥 Known Entities", ""]
        for e in entities:
            note = f" — {_md_inline(e['notes'])}" if e["notes"] else ""
            lines.append(f"- **{_md_inline(e['name'])}** ({e['entity_type']}){note}")
        lines.append("")

    lines += [
        "---",
        "_Commands: `Search-Memory -Query 'term'` | `Add-Fact -TopicSlug 'slug' -Content '...' -Importance 5`_",
    ]

    content = "\n".join(lines)
    os.makedirs(os.path.dirname(ctx_path), exist_ok=True)
    with open(ctx_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    return {
        "path": ctx_path,
        "topics_loaded": len(topics),
        "facts_loaded": len(facts),
        "snapshots_loaded": len(snapshots),
        "pending_loaded": len(pending),
        "entities_loaded": len(entities),
        "generated_at": ts,
    }


def batch(p):
    """Run several ops inside a single interpreter start.

    Python process startup dominates real-world latency: on a typical Windows
    box `python -c pass` costs ~435 ms and importing this module ~519 ms, while
    the database work behind a session start is ~39 ms. Commands that chained
    several ops therefore paid the interpreter cost several times over.
    Batching collapses that to one spawn.

    Each step is a full op payload; db_path / context_path set at the top level
    are inherited unless a step overrides them. A failing step is captured
    rather than aborting the batch.
    """
    results = []
    for step in p.get("ops", []):
        op = step.get("op")
        fn = DISPATCH.get(op)
        if fn is None:
            results.append({"error": f"unknown op: {op}"})
            continue
        merged = dict(step)
        for inherited in ("db_path", "context_path"):
            if p.get(inherited) and inherited not in merged:
                merged[inherited] = p[inherited]
        try:
            results.append(fn(merged))
        except Exception as exc:                     # noqa: BLE001 - reported, not raised
            results.append({"error": str(exc), "type": type(exc).__name__})
    return {"results": results}


def get_stats(p=None):
    """Quick stats for debugging / status display."""
    with get_conn(_db_path(p or {})) as conn:
        return {
            "topics": conn.execute("SELECT count(*) FROM topics").fetchone()[0],
            "facts":  conn.execute("SELECT count(*) FROM facts").fetchone()[0],
            "entities": conn.execute("SELECT count(*) FROM entities").fetchone()[0],
            "snapshots": conn.execute("SELECT count(*) FROM snapshots").fetchone()[0],
            "refs": conn.execute("SELECT count(*) FROM refs").fetchone()[0],
            "schema_version": conn.execute("SELECT version FROM schema_version").fetchone()[0],
        }


# ── dispatch ──────────────────────────────────────────────────────────

DISPATCH = {
    "add_topic":      add_topic,
    "get_topic":      get_topic,
    "add_fact":       add_fact,
    "add_entity":     add_entity,
    "add_snapshot":   add_snapshot,
    "set_identity":   set_identity,
    "touch_fact":     touch_fact,
    "touch_topic":    touch_topic,
    "search_memory":  search_memory,
    "export_context": export_context,
    "migrate":        migrate,
    "reindex_fts":    reindex_fts,
    "optimize_db":    optimize_db,
    "batch":          batch,
    "get_stats":      get_stats,
}

if __name__ == "__main__":
    _ensure_utf8_stdin()
    try:
        payload = json.loads(sys.stdin.read())
        op = payload.get("op")
        if op not in DISPATCH:
            print(json.dumps({"error": f"Unknown op: {op}. Valid ops: {list(DISPATCH.keys())}"}))
            sys.exit(1)
        result = DISPATCH[op](payload)
        print(json.dumps(result, default=str))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        sys.exit(1)

