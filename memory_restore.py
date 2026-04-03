#!/usr/bin/env python3
r"""
memory_restore.py — Restore memory.db from a JSON backup on a new machine.

Usage:
  python C:\workspace\agency\long-term-memory\memory_restore.py memory-backup-20260330-184013.json
  python memory_restore.py backup.json --db C:\Users\newuser\.copilot\memory.db

After restore, automatically runs export_context to regenerate memory-context.md.
Idempotent: uses ON CONFLICT DO NOTHING to avoid duplicate rows on partial re-import.
"""
import argparse
import json
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.environ["USERPROFILE"], ".copilot", "memory.db")
INIT_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "init-memory.sql")
DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_driver.py")


def init_schema(db_path):
    """Create the schema if memory.db doesn't exist or is empty."""
    if not os.path.exists(INIT_SQL):
        print(f"ERROR: init-memory.sql not found at {INIT_SQL}", file=sys.stderr)
        sys.exit(1)
    sql = open(INIT_SQL, encoding="utf-8").read()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(sql)
    print(f"Schema initialized: {db_path}")


def restore(backup_path, db_path):
    with open(backup_path, "r", encoding="utf-8") as f:
        backup = json.load(f)
    if not isinstance(backup, dict) or "tables" not in backup:
        print("ERROR: backup file missing top-level 'tables' object", file=sys.stderr)
        sys.exit(1)

    fmt = backup.get("export_format", "unknown")
    if not fmt.startswith("copilot-memory"):
        print(f"WARNING: Unrecognized backup format '{fmt}'. Proceeding anyway.", file=sys.stderr)

    exported_at = backup.get("exported_at", "unknown")
    print(f"Backup format: {fmt}")
    print(f"Exported at:   {exported_at}")
    print(f"Restoring to:  {db_path}\n")

    # Always ensure schema is current (init script is idempotent).
    init_schema(db_path)
    if os.path.exists(db_path):
        print("DB ready — merging backup rows (duplicates ignored).\n")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # off during bulk restore to avoid order issues
    conn.execute("PRAGMA journal_mode = WAL")

    # Restore order matters for FK integrity
    # (foreign_keys=OFF means we can restore in any order, but let's be clean)
    table_order = ["schema_version", "topics", "entities", "facts",
                   "entity_mentions", "snapshots", "refs"]

    total_inserted = 0
    for table in table_order:
        rows = backup.get("tables", {}).get(table, [])
        if not rows:
            print(f"  {table}: (empty, skipping)")
            continue

        cols = list(rows[0].keys())
        placeholders = ",".join(["?"] * len(cols))
        col_str = ",".join(cols)
        sql = f"INSERT OR IGNORE INTO {table}({col_str}) VALUES({placeholders})"

        inserted = 0
        for row in rows:
            values = [row.get(c) for c in cols]
            try:
                conn.execute(sql, values)
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception as e:
                print(f"  WARNING: could not insert into {table}: {e} | row: {row}", file=sys.stderr)

        conn.commit()
        print(f"  {table}: {len(rows)} rows in backup, {inserted} newly inserted")
        total_inserted += inserted

    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()

    print(f"\nTotal new rows inserted: {total_inserted}")

    # Regenerate memory-context.md
    print("\nRegenerating memory-context.md...")
    import subprocess
    context_path = os.path.join(os.path.dirname(db_path), "memory-context.md")
    result = subprocess.run(
        [sys.executable, DRIVER],
        input=json.dumps({
            "op": "export_context",
            "db_path": db_path,
            "context_path": context_path,
        }),
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode == 0:
        ctx = json.loads(result.stdout)
        print(f"✅ Context file: {ctx.get('path', '?')} "
              f"({ctx.get('topics_loaded', 0)} topics, {ctx.get('facts_loaded', 0)} facts)")
    else:
        print(f"WARNING: context export failed: {result.stderr}", file=sys.stderr)

    print(f"\n✅ Restore complete → {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore memory.db from JSON backup")
    parser.add_argument("backup", help="Path to the backup JSON file")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Destination DB path (default: {DEFAULT_DB})")
    args = parser.parse_args()

    if not os.path.exists(args.backup):
        print(f"ERROR: Backup file not found: {args.backup}", file=sys.stderr)
        sys.exit(1)

    restore(args.backup, args.db)
