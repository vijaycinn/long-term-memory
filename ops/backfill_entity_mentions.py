#!/usr/bin/env python3
"""Backfill fact-to-entity mention edges without changing source facts.

Dry run is the default. Use ``--apply`` only after reviewing the candidate
sample. Rollback an apply with:

    DELETE FROM entity_mentions WHERE parent_type='fact' AND created_at >= '<stamp>'
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

_REPO = Path(__file__).resolve().parent.parent
_WIKI = str(_REPO / "wiki")
if _WIKI not in sys.path:
    sys.path.insert(0, _WIKI)

import config
from notes import STOP_ALIASES


def is_matchable_alias(name: str) -> bool:
    """Apply the alias eligibility rule used by ``wiki.notes.register``."""
    alias = name.strip().lower()
    # Short aliases and generic stop aliases create high-volume false positives;
    # retain notes.register's discipline rather than broadening entity edges.
    return len(alias) >= 4 and alias not in STOP_ALIASES


def compile_entity_matchers(entities: Iterable[sqlite3.Row]) -> list[tuple[int, str, re.Pattern[str]]]:
    """Build boundary-aware matchers for entities eligible as wiki aliases."""
    matchers = []
    for entity in entities:
        name = entity["name"].strip()
        if is_matchable_alias(name):
            # A non-word guard is a word boundary that also handles punctuation
            # in entity names without allowing a match inside a larger token.
            pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
            matchers.append((entity["id"], name, pattern))
    return matchers


def matching_entity_ids(text: str, matchers: Iterable[tuple[int, str, re.Pattern[str]]]) -> list[int]:
    """Return entity ids whose eligible aliases occur as complete words."""
    return [entity_id for entity_id, _name, pattern in matchers if pattern.search(text)]


def _candidate_edges(conn: sqlite3.Connection, facts: Iterable[sqlite3.Row]) -> list[dict]:
    entities = conn.execute("SELECT id, name FROM entities ORDER BY name COLLATE NOCASE, id").fetchall()
    matchers = compile_entity_matchers(entities)
    name_by_id = {entity["id"]: entity["name"] for entity in entities}
    candidates = []
    for fact in facts:
        for entity_id in matching_entity_ids(fact["content"], matchers):
            candidates.append({
                "entity_id": entity_id,
                "entity_name": name_by_id[entity_id],
                "fact_id": fact["id"],
                "content": fact["content"],
            })
    return candidates


def record_fact_entity_mentions(
    conn: sqlite3.Connection, facts: Iterable[sqlite3.Row]
) -> dict:
    """Insert fact edges for supplied fact rows and return their edge totals.

    Callers provide only newly written facts so session-end processing never
    rescans the full fact table.
    """
    candidates = _candidate_edges(conn, facts)
    existing = 0
    inserted = 0
    for candidate in candidates:
        exists = conn.execute(
            """SELECT 1 FROM entity_mentions
               WHERE entity_id=? AND parent_id=? AND parent_type='fact' AND relationship='mentions'""",
            (candidate["entity_id"], candidate["fact_id"]),
        ).fetchone()
        if exists:
            existing += 1
            continue
        cursor = conn.execute(
            """INSERT OR IGNORE INTO entity_mentions
               (entity_id, parent_id, parent_type, relationship) VALUES (?, ?, 'fact', 'mentions')""",
            (candidate["entity_id"], candidate["fact_id"]),
        )
        inserted += cursor.rowcount
    return {
        "facts_scanned": len({candidate["fact_id"] for candidate in candidates}),
        "candidate_edges": len(candidates),
        "edges_already_exist": existing,
        "edges_inserted": inserted,
        "sample": candidates[:20],
    }


def run_backfill(
    db_path: str | None = None, apply: bool = False, backup_dir: str | None = None
) -> dict:
    """Measure all active-fact edges and optionally insert them."""
    paths = config.paths(memory_db=db_path)
    resolved_db = paths["memory_db"]
    if not resolved_db.is_file():
        return {"error": f"database not found: {resolved_db}"}

    conn = sqlite3.connect(resolved_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        facts = conn.execute(
            "SELECT id, content FROM facts WHERE status='active' ORDER BY id"
        ).fetchall()
        candidates = _candidate_edges(conn, facts)
        existing_keys = {
            (row["entity_id"], row["parent_id"])
            for row in conn.execute(
                """SELECT entity_id, parent_id FROM entity_mentions
                   WHERE parent_type='fact' AND relationship='mentions'"""
            ).fetchall()
        }
        existing = sum(
            (candidate["entity_id"], candidate["fact_id"]) in existing_keys
            for candidate in candidates
        )
        facts_with_candidates = len({candidate["fact_id"] for candidate in candidates})
        stamp = None
        inserted = 0
        backup = None
        if apply:
            import ltm_backup

            backup_payload = {"db_path": str(resolved_db)}
            if backup_dir:
                backup_payload["backup_dir"] = backup_dir
            backup = ltm_backup.backup(backup_payload)
            if not backup.get("ok"):
                return {"error": "backup failed; refusing to apply", "backup": backup}
            stamp = conn.execute("SELECT datetime('now')").fetchone()[0]
            for candidate in candidates:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO entity_mentions
                       (entity_id, parent_id, parent_type, relationship)
                       VALUES (?, ?, 'fact', 'mentions')""",
                    (candidate["entity_id"], candidate["fact_id"]),
                )
                inserted += cursor.rowcount
            conn.commit()

        return {
            "db_path": str(resolved_db),
            "dry_run": not apply,
            "facts_scanned": len(facts),
            "facts_with_candidate_edges": facts_with_candidates,
            "candidate_edges": len(candidates),
            "edges_already_exist": existing,
            "edges_would_be_inserted": len(candidates) - existing,
            "edges_inserted": inserted if apply else 0,
            "sample": [
                {
                    "entity_name": candidate["entity_name"],
                    "fact_id": candidate["fact_id"],
                    "content": candidate["content"][:120],
                }
                for candidate in candidates[:20]
            ],
            "backup": backup,
            "rollback_stamp": stamp,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill fact entity mentions.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report candidates (default).")
    mode.add_argument("--apply", action="store_true", help="Back up and insert candidates.")
    parser.add_argument("--db-path", help="Override the configured memory database path.")
    parser.add_argument("--backup-dir", help="Override the backup directory for this apply.")
    args = parser.parse_args()

    preview = run_backfill(db_path=args.db_path, apply=False)
    if preview.get("error"):
        print(json.dumps(preview))
        return 1
    # The audit's expected 251 measures facts with latent edges, while a fact
    # can correctly yield several entity-to-fact rows. Guard that comparable
    # count, not the potentially larger physical edge count.
    if args.apply and not 200 <= preview["facts_with_candidate_edges"] <= 320:
        preview["error"] = (
            "fact coverage outside expected 200-320 range; refusing to apply"
        )
        print(json.dumps(preview, default=str))
        return 1

    # Emit the review sample before an apply can mutate the database.
    print(json.dumps(preview, default=str))
    if not args.apply:
        return 0

    result = run_backfill(
        db_path=args.db_path, apply=True, backup_dir=args.backup_dir
    )
    print(json.dumps(result, default=str))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
