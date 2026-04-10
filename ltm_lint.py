#!/usr/bin/env python3
"""
ltm_lint.py — Health-check the long-term memory database.

Reads JSON config from stdin (supports db_path override, otherwise ~/.copilot/memory.db).
Prints a JSON report to stdout with check results and an overall healthy flag.

Usage:
    echo '{}' | python ltm_lint.py
    echo '{"db_path": "/path/to/memory.db"}' | python ltm_lint.py
"""

import json
import sys
import sqlite3
import os
import io
import re

sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

DEFAULT_DB = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                          ".copilot", "memory.db")

STALE_FACT_DAYS = 60
STALE_TOPIC_DAYS = 90
TODO_AGE_DAYS = 14


def _db_path(payload):
    return payload.get("db_path") or DEFAULT_DB


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── health checks ─────────────────────────────────────────────────────

def check_stale_facts(conn):
    rows = conn.execute(
        """
        SELECT f.id AS fact_id,
               SUBSTR(f.content, 1, 80) AS content,
               t.slug AS topic_slug,
               CAST(julianday('now') - julianday(COALESCE(f.last_accessed_at, f.created_at))
                    AS INTEGER) AS days_stale
        FROM facts f
        LEFT JOIN topics t ON t.id = f.topic_id
        WHERE f.status = 'active'
          AND f.importance >= 3
          AND (f.last_accessed_at IS NULL
               OR julianday('now') - julianday(f.last_accessed_at) > ?)
        ORDER BY days_stale DESC
        """,
        (STALE_FACT_DAYS,),
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def check_stale_topics(conn):
    rows = conn.execute(
        """
        SELECT slug, title,
               CAST(julianday('now') - julianday(COALESCE(last_accessed_at, created_at))
                    AS INTEGER) AS days_stale
        FROM topics
        WHERE status = 'active'
          AND (last_accessed_at IS NULL
               OR julianday('now') - julianday(last_accessed_at) > ?)
        ORDER BY days_stale DESC
        """,
        (STALE_TOPIC_DAYS,),
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def check_orphaned_entities(conn):
    rows = conn.execute(
        """
        SELECT e.id AS entity_id, e.name, e.entity_type
        FROM entities e
        LEFT JOIN entity_mentions em ON em.entity_id = e.id
        WHERE em.id IS NULL
        """
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def check_empty_topics(conn):
    rows = conn.execute(
        """
        SELECT t.slug, t.title, t.category
        FROM topics t
        LEFT JOIN facts f ON f.topic_id = t.id
        WHERE f.id IS NULL
        """
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


_NEGATION_PAIRS = [
    (re.compile(r"\buse\b", re.I), re.compile(r"\bdon'?t use\b", re.I)),
    (re.compile(r"\balways\b", re.I), re.compile(r"\bnever\b", re.I)),
    (re.compile(r"\benable\b", re.I), re.compile(r"\bdisable\b", re.I)),
    (re.compile(r"\bprefer\b", re.I), re.compile(r"\bavoid\b", re.I)),
]


def check_contradictions(conn):
    """Heuristic: find decision-type facts in the same topic with opposing phrases."""
    topics = conn.execute(
        "SELECT DISTINCT topic_id FROM facts "
        "WHERE fact_type = 'decision' AND status = 'active' AND topic_id IS NOT NULL"
    ).fetchall()

    items = []
    for (topic_id,) in topics:
        facts = conn.execute(
            "SELECT id, content FROM facts "
            "WHERE topic_id = ? AND fact_type = 'decision' AND status = 'active'",
            (topic_id,),
        ).fetchall()

        slug_row = conn.execute(
            "SELECT slug FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        slug = slug_row["slug"] if slug_row else str(topic_id)

        for i, a in enumerate(facts):
            for b in facts[i + 1:]:
                for pos_re, neg_re in _NEGATION_PAIRS:
                    a_pos = bool(pos_re.search(a["content"]))
                    a_neg = bool(neg_re.search(a["content"]))
                    b_pos = bool(pos_re.search(b["content"]))
                    b_neg = bool(neg_re.search(b["content"]))
                    if (a_pos and b_neg) or (a_neg and b_pos):
                        items.append({
                            "topic_slug": slug,
                            "fact_a": {"id": a["id"], "content": a["content"][:80]},
                            "fact_b": {"id": b["id"], "content": b["content"][:80]},
                        })
                        break  # one match per pair is enough

    return {"count": len(items), "items": items}


def check_superseded_in_active(conn):
    rows = conn.execute(
        """
        SELECT f.id AS fact_id, SUBSTR(f.content, 1, 80) AS content
        FROM facts f
        JOIN topics t ON t.id = f.topic_id
        WHERE f.status = 'superseded'
          AND t.status = 'active'
        """
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def check_unresolved_todos(conn):
    rows = conn.execute(
        """
        SELECT f.id AS fact_id,
               SUBSTR(f.content, 1, 80) AS content,
               CAST(julianday('now') - julianday(f.created_at) AS INTEGER) AS days_old,
               t.slug AS topic_slug
        FROM facts f
        LEFT JOIN topics t ON t.id = f.topic_id
        WHERE f.fact_type = 'todo'
          AND f.status = 'active'
          AND julianday('now') - julianday(f.created_at) > ?
        ORDER BY days_old DESC
        """,
        (TODO_AGE_DAYS,),
    ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


def check_fts_sync(conn):
    """Compare FTS row count against source tables (facts + entities + topics + snapshots)."""
    fts_count = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
    src_count = 0
    for tbl in ("facts", "entities", "topics", "snapshots"):
        src_count += conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
    return {
        "in_sync": fts_count == src_count,
        "expected": src_count,
        "actual": fts_count,
    }


# ── main ──────────────────────────────────────────────────────────────

def run_lint(payload=None):
    payload = payload or {}
    db_path = _db_path(payload)

    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}"}

    conn = get_conn(db_path)
    try:
        checks = {
            "stale_facts":         check_stale_facts(conn),
            "stale_topics":        check_stale_topics(conn),
            "orphaned_entities":   check_orphaned_entities(conn),
            "empty_topics":        check_empty_topics(conn),
            "contradictions":      check_contradictions(conn),
            "superseded_in_active": check_superseded_in_active(conn),
            "unresolved_todos":    check_unresolved_todos(conn),
            "fts_sync":            check_fts_sync(conn),
        }
    finally:
        conn.close()

    # Build summary from non-zero checks (skip fts_sync — it has its own format)
    parts = []
    for name, result in checks.items():
        if name == "fts_sync":
            if not result["in_sync"]:
                parts.append(f"FTS drift (expected {result['expected']}, actual {result['actual']})")
        elif result["count"] > 0:
            label = name.replace("_", " ")
            parts.append(f"{result['count']} {label}")

    healthy = len(parts) == 0
    summary = ", ".join(parts) if parts else "all checks passed"

    return {
        "healthy": healthy,
        "checks": checks,
        "summary": summary,
    }


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = run_lint(payload)
        print(json.dumps(result, default=str))
        sys.exit(0 if "error" not in result else 1)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        sys.exit(1)
