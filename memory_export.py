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

    tables = ["schema_version", "topics", "facts", "entities",
              "entity_mentions", "snapshots", "refs"]

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export memory.db to JSON backup")
    parser.add_argument("--output", "-o", help="Output file path (default: auto-timestamped in .copilot/)")
    args = parser.parse_args()
    print(f"Exporting memory.db from: {DB}\n")
    export_memory(args.output)
