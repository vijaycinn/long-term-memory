#!/usr/bin/env python3
"""
ltm_maintain.py — Retention and compaction for the long-term memory store.

ltm_lint.py reports problems; this module fixes them. Without it the database
only ever grows: every captured fact, every abandoned topic and every
superseded row stays forever, which slowly degrades both query latency and the
signal-to-noise ratio of the bridge.

Operations (all reversible except prune, all no-ops unless thresholds are met):

    archive_stale_topics   topics untouched for N days -> status='archived'
    prune_superseded       superseded/disproven/resolved facts older than N days -> deleted
    dedupe_facts           byte-identical content within the same topic -> keep oldest
    compact                WAL checkpoint + VACUUM + ANALYZE

DRY RUN IS THE DEFAULT. Pass {"apply": true} to actually write.

Usage:
    echo '{}'                          | python ltm_maintain.py   # report only
    echo '{"apply": true}'             | python ltm_maintain.py   # execute
    echo '{"apply": true, "stale_topic_days": 365}' | python ltm_maintain.py
"""

import io
import json
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".copilot", "memory.db")

DEFAULT_STALE_TOPIC_DAYS = 180
DEFAULT_PRUNE_DAYS = 90
PRUNABLE_STATUSES = ("superseded", "disproven", "resolved")


def _ensure_utf8_stdin():
    """Force UTF-8 stdin. Called from __main__ only — see memory_driver.py."""
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "buffer"):
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    except (ValueError, AttributeError):
        pass


def _db_path(p):
    return (p or {}).get("db_path") or DEFAULT_DB


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── operations ────────────────────────────────────────────────────────

def archive_stale_topics(conn, days, apply_changes):
    """Archive topics nobody has touched in `days`, protecting ones with fresh facts.

    Archived topics keep all their data and stay searchable; they simply stop
    competing for space in memory-context.md.
    """
    rows = conn.execute(
        """
        SELECT t.id, t.slug, t.title,
               CAST(julianday('now') - julianday(COALESCE(t.last_accessed_at, t.created_at))
                    AS INTEGER) AS idle_days
        FROM topics t
        WHERE t.status = 'active'
          AND julianday('now') - julianday(COALESCE(t.last_accessed_at, t.created_at)) > ?
          AND NOT EXISTS (
              SELECT 1 FROM facts f
              WHERE f.topic_id = t.id
                AND f.status = 'active'
                AND julianday('now') - julianday(COALESCE(f.last_accessed_at, f.created_at)) <= ?
          )
        ORDER BY idle_days DESC
        """,
        (days, days),
    ).fetchall()

    if apply_changes and rows:
        conn.executemany(
            "UPDATE topics SET status='archived' WHERE id=?", [(r["id"],) for r in rows]
        )
    return {
        "count": len(rows),
        "threshold_days": days,
        "items": [{"slug": r["slug"], "idle_days": r["idle_days"]} for r in rows[:20]],
    }


def prune_superseded(conn, days, apply_changes):
    """Delete facts already marked superseded/disproven/resolved and gone cold.

    This is the only destructive operation. Facts must be BOTH explicitly
    retired AND untouched for `days`, so nothing active is ever removed.
    """
    placeholders = ",".join("?" * len(PRUNABLE_STATUSES))
    rows = conn.execute(
        f"""
        SELECT id, status, SUBSTR(content, 1, 60) AS preview
        FROM facts
        WHERE status IN ({placeholders})
          AND julianday('now') - julianday(COALESCE(last_accessed_at, created_at)) > ?
        """,
        (*PRUNABLE_STATUSES, days),
    ).fetchall()

    if apply_changes and rows:
        conn.executemany("DELETE FROM facts WHERE id=?", [(r["id"],) for r in rows])
    return {
        "count": len(rows),
        "threshold_days": days,
        "items": [{"id": r["id"], "status": r["status"], "preview": r["preview"]}
                  for r in rows[:20]],
    }


def dedupe_facts(conn, apply_changes):
    """Remove byte-identical duplicates, keeping the oldest row.

    UNIQUE(topic_id, content) does not catch global facts because SQLite treats
    NULL != NULL, so topic_id IS NULL rows can duplicate freely.
    """
    rows = conn.execute(
        """
        SELECT MIN(id) AS keep_id, COUNT(*) AS n, topic_id, SUBSTR(content,1,60) AS preview
        FROM facts
        GROUP BY topic_id, content
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    removed = 0
    details = []
    for r in rows:
        dupes = conn.execute(
            "SELECT id FROM facts WHERE topic_id IS ? AND content = "
            "(SELECT content FROM facts WHERE id=?) AND id != ?",
            (r["topic_id"], r["keep_id"], r["keep_id"]),
        ).fetchall()
        removed += len(dupes)
        details.append({"keep_id": r["keep_id"], "removed": len(dupes),
                        "preview": r["preview"]})
        if apply_changes and dupes:
            conn.executemany("DELETE FROM facts WHERE id=?", [(d["id"],) for d in dupes])
    return {"count": removed, "groups": len(rows), "items": details[:20]}


def compact(db_path, apply_changes):
    """Checkpoint the WAL, reclaim free pages, refresh planner statistics.

    VACUUM cannot run inside a transaction, so it uses its own connection with
    isolation_level=None.
    """
    before = os.path.getsize(db_path)
    if not apply_changes:
        return {"applied": False, "size_before_bytes": before}

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    finally:
        conn.close()
    after = os.path.getsize(db_path)
    return {
        "applied": True,
        "size_before_bytes": before,
        "size_after_bytes": after,
        "reclaimed_bytes": before - after,
        "reclaimed_mb": round((before - after) / (1024 * 1024), 2),
    }


def run_maintenance(payload=None):
    p = payload or {}
    db_path = _db_path(p)
    apply_changes = bool(p.get("apply", False))
    stale_days = int(p.get("stale_topic_days", DEFAULT_STALE_TOPIC_DAYS))
    prune_days = int(p.get("prune_days", DEFAULT_PRUNE_DAYS))

    if not os.path.exists(db_path):
        return {"error": f"database not found: {db_path}"}

    conn = get_conn(db_path)
    try:
        before = {
            "topics_active": conn.execute(
                "SELECT count(*) FROM topics WHERE status='active'").fetchone()[0],
            "facts_active": conn.execute(
                "SELECT count(*) FROM facts WHERE status='active'").fetchone()[0],
            "facts_total": conn.execute("SELECT count(*) FROM facts").fetchone()[0],
            "fts_rows": conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0],
        }
        result = {
            "db_path": db_path,
            "dry_run": not apply_changes,
            "before": before,
            "archive_stale_topics": archive_stale_topics(conn, stale_days, apply_changes),
            "prune_superseded": prune_superseded(conn, prune_days, apply_changes),
            "dedupe_facts": dedupe_facts(conn, apply_changes),
        }
        if apply_changes:
            conn.commit()
    finally:
        conn.close()

    result["compact"] = compact(db_path, apply_changes)

    conn = get_conn(db_path)
    try:
        result["after"] = {
            "topics_active": conn.execute(
                "SELECT count(*) FROM topics WHERE status='active'").fetchone()[0],
            "facts_active": conn.execute(
                "SELECT count(*) FROM facts WHERE status='active'").fetchone()[0],
            "facts_total": conn.execute("SELECT count(*) FROM facts").fetchone()[0],
            "fts_rows": conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0],
        }
    finally:
        conn.close()
    return result


if __name__ == "__main__":
    _ensure_utf8_stdin()
    try:
        raw = sys.stdin.read() or "{}"
        print(json.dumps(run_maintenance(json.loads(raw)), default=str))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        sys.exit(1)
