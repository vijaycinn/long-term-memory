#!/usr/bin/env python3
"""
memory_export.py — Export memory.db to a portable JSON backup.

Usage:
  python memory_export.py
  python memory_export.py --output custom-backup.json

Creates: memory-backup-YYYYMMDD-HHMMSS.json in the same folder as memory.db.
The backup is human-readable and fully restorable via memory_restore.py.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB = os.path.join(os.environ["USERPROFILE"], ".copilot", "memory.db")


def _ensure_utf8_stdout():
    """Force UTF-8 stdout so status output cannot crash the export.

    The console codepage on Windows is cp1252 by default, and printing a single
    emoji raised UnicodeEncodeError *after* the backup file had already been
    written — making a successful export look like a failure to any caller
    checking the exit code.
    """
    for stream in ("stdout", "stderr"):
        try:
            getattr(sys, stream).reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _ordered_select_sql(conn, table):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if "id" in cols:
        return f"SELECT * FROM {table} ORDER BY id"
    if table == "schema_version":
        return f"SELECT * FROM {table} ORDER BY version"
    return f"SELECT * FROM {table}"


def export_memory(output_path=None):
    if not os.path.exists(DB):
        print(f"ERROR: memory.db not found at {DB}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if output_path is None:
        db_dir = os.path.dirname(DB)
        output_path = os.path.join(db_dir, f"memory-backup-{ts}.json")

    # memory_fts* are derived and rebuildable, so they are intentionally omitted.
    # ltm_patterns / ltm_session_log are NOT derived — they are behavioural
    # memory, and leaving them out silently dropped real signal.
    tables = ["schema_version", "topics", "facts", "entities",
              "entity_mentions", "snapshots", "refs",
              "ltm_patterns", "ltm_session_log"]

    backup = {
        "export_format": "copilot-memory-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": os.path.basename(DB),
        "tables": {}
    }

    for table in tables:
        try:
            rows = conn.execute(_ordered_select_sql(conn, table)).fetchall()
            if rows:
                backup["tables"][table] = [dict(row) for row in rows]
            else:
                backup["tables"][table] = []
            print(f"  {table}: {len(backup['tables'][table])} rows")
        except Exception as e:
            print(f"  WARNING: could not export {table}: {e}", file=sys.stderr)
            backup["tables"][table] = []

    conn.close()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, default=str, ensure_ascii=False)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\n✅ Backup saved: {output_path} ({size_kb:.1f} KB)")
    return output_path


def export_portable(output_path=None):
    """Flat, join-free export for consumption by a different agent harness.

    The default export mirrors the SQL tables, so facts reference topics by
    numeric id and any consumer has to reproduce the joins. This variant
    resolves those references and drops internal ids, producing a structure a
    foreign harness can read directly.

    Not restorable by memory_restore.py — use the standard export for recovery.
    """
    if not os.path.exists(DB):
        print(f"ERROR: memory.db not found at {DB}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if output_path is None:
        output_path = os.path.join(os.path.dirname(DB), f"memory-portable-{ts}.json")

    def rows(sql, args=()):
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.Error:
            return []

    identity = rows(
        "SELECT name, entity_type AS type, attributes, notes FROM entities "
        "WHERE is_self=1 LIMIT 1")

    out = {
        "export_format": "copilot-memory-portable-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": os.path.basename(DB),
        "identity": identity[0] if identity else None,
        "topics": rows(
            "SELECT slug, title, category, status, description, "
            "last_accessed_at, created_at FROM topics ORDER BY slug"),
        "facts": rows(
            "SELECT f.content, f.fact_type AS type, f.importance, f.confidence, "
            "       f.status, f.source, COALESCE(t.slug,'') AS topic, "
            "       f.last_accessed_at, f.created_at "
            "FROM facts f LEFT JOIN topics t ON t.id = f.topic_id "
            "ORDER BY f.importance DESC, f.id"),
        "entities": rows(
            "SELECT name, entity_type AS type, notes, attributes FROM entities "
            "WHERE is_self=0 ORDER BY name"),
        "snapshots": rows(
            "SELECT COALESCE(t.slug,'') AS topic, s.seq_number, s.title, s.summary, "
            "       s.findings, s.decisions, s.open_questions, s.next_steps, s.created_at "
            "FROM snapshots s LEFT JOIN topics t ON t.id = s.topic_id "
            "ORDER BY t.slug, s.seq_number"),
        "patterns": rows(
            "SELECT pattern_type, description, cwd, created_at "
            "FROM ltm_patterns ORDER BY created_at DESC"),
        "sessions": rows(
            "SELECT session_id, summary, cwd, end_reason, facts_extracted, "
            "       entities_extracted, patterns_detected, processed_at "
            "FROM ltm_session_log ORDER BY processed_at DESC"),
    }
    conn.close()

    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str, ensure_ascii=False)

    for k, v in out["counts"].items():
        print(f"  {k}: {v}")
    size_kb = os.path.getsize(output_path) / 1024
    print(f"\n✅ Portable export saved: {output_path} ({size_kb:.1f} KB)")
    return output_path


if __name__ == "__main__":
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="Export memory.db to JSON backup")
    parser.add_argument("--output", "-o", help="Output file path (default: auto-timestamped in .copilot/)")
    parser.add_argument("--portable", action="store_true",
                        help="Flat, join-free export for a different agent harness "
                             "(topic slugs resolved, ids dropped). Not restorable.")
    args = parser.parse_args()
    print(f"Exporting memory.db from: {DB}\n")
    if args.portable:
        export_portable(args.output)
    else:
        export_memory(args.output)
