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

# Ensure UTF-8 stdin regardless of Windows console codepage
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

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
    with get_conn(_db_path(p)) as conn:
        conn.execute(
            """
            INSERT INTO topics(slug, title, category, description, status)
            VALUES(?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                title = CASE WHEN excluded.title != slug THEN excluded.title ELSE topics.title END,
                description = COALESCE(NULLIF(excluded.description,''), topics.description),
                status = excluded.status
            """,
            (slug, title, category, desc, status),
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
    with get_conn(_db_path(p)) as conn:
        # SQLite UNIQUE(topic_id, content) treats NULL != NULL, so global facts (topic_id=NULL)
        # are NOT caught by the constraint. Always pre-check with IS for NULL-safe comparison.
        existing = conn.execute(
            "SELECT id FROM facts WHERE topic_id IS ? AND content=?", (topic_id, content)
        ).fetchone()
        if existing:
            return {"fact_id": existing[0], "inserted": False}
        cur = conn.execute(
            """
            INSERT INTO facts
              (topic_id, content, fact_type, confidence, importance, source, session_id)
            VALUES(?,?,?,?,?,?,?)
            """,
            (topic_id, content, fact_type, confidence, importance, source, session_id),
        )
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


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def search_memory(p):
    query = p["query"]
    limit = p.get("limit", 10)
    with get_conn(_db_path(p)) as conn:
        rows = conn.execute(
            """
            SELECT r.src, r.id, r.text, r.subtype, bm25(memory_fts) AS rank
            FROM memory_fts m
            JOIN fts_resolved r ON r.src = m.table_name AND r.id = m.row_id
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def export_context(p=None):
    """Tier-1 + Tier-2 export to memory-context.md. Side effect: updates last_accessed_at."""
    tier1_limit = 5
    tier2_limit = 20

    db_path = _db_path(p or {})
    ctx_path = _ctx_path(p or {})

    with get_conn(db_path) as conn:
        # Tier 1-a: self entity
        self_entity = conn.execute(
            "SELECT name, entity_type, attributes, notes FROM entities WHERE is_self=1 LIMIT 1"
        ).fetchone()

        # Tier 1-b: top active topics by recency
        topics = conn.execute(
            """
            SELECT id, slug, title, category, description, last_accessed_at
            FROM topics WHERE status='active'
            ORDER BY last_accessed_at DESC
            LIMIT ?
            """,
            (tier1_limit,),
        ).fetchall()

        # Tier 2: importance >= 4 facts, across all topics
        facts = conn.execute(
            """
            SELECT f.id, f.content, f.fact_type, f.importance, f.confidence,
                   f.source, t.slug AS topic_slug
            FROM facts f
            LEFT JOIN topics t ON t.id = f.topic_id
            WHERE f.status='active' AND f.importance >= 4
            ORDER BY f.importance DESC, f.last_accessed_at DESC
            LIMIT ?
            """,
            (tier2_limit,),
        ).fetchall()

        # Side effect: touch loaded topics
        now = _now()
        for t in topics:
            conn.execute(
                "UPDATE topics SET last_accessed_at=? WHERE id=?", (now, t[0])
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
        lines.append(f"- **Name**: {self_entity['name']}")
        if self_entity["attributes"]:
            lines.append(f"- **Attributes**: {self_entity['attributes']}")
        if self_entity["notes"]:
            lines.append(f"- **Notes**: {self_entity['notes']}")
        lines.append("")

    if topics:
        lines += ["## 📂 Active Topics (recently accessed)", ""]
        for t in topics:
            ts_accessed = t["last_accessed_at"] or "never"
            lines.append(f"### {t['title']}")
            lines.append(
                f"- **Slug**: `{t['slug']}` | **Category**: {t['category']} | **Last accessed**: {ts_accessed}"
            )
            if t["description"]:
                lines.append(f"- {t['description']}")
            lines.append("")

    if facts:
        lines += ["## ⭐ High-Priority Facts (importance ≥ 4)", ""]
        by_topic = {}
        for f in facts:
            slug = f["topic_slug"] or "general"
            by_topic.setdefault(slug, []).append(f)
        for slug, topic_facts in by_topic.items():
            lines.append(f"### [{slug}]")
            for f in topic_facts:
                star = "⭐" * f["importance"]
                lines.append(f"- {star} **[{f['fact_type']}]** {f['content']}")
                if f["source"]:
                    lines.append(f"  _Source: {f['source']}_")
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
        "generated_at": ts,
    }


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
    "touch_fact":     touch_fact,
    "touch_topic":    touch_topic,
    "search_memory":  search_memory,
    "export_context": export_context,
    "get_stats":      get_stats,
}

if __name__ == "__main__":
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
