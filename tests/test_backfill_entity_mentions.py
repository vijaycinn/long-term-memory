#!/usr/bin/env python3
"""Tests for the fact entity edge backfill."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _folder in ("ops", "wiki"):
    _path = str(_REPO / _folder)
    if _path not in __import__("sys").path:
        __import__("sys").path.insert(0, _path)

from backfill_entity_mentions import run_backfill

INIT_SQL = (Path(__file__).resolve().parent.parent / "setup" / "init-memory.sql").read_text(
    encoding="utf-8"
)


class TestBackfillEntityMentions(unittest.TestCase):
    def test_dry_run_and_apply_use_boundary_aware_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(INIT_SQL)
            conn.execute("INSERT INTO entities(name, entity_type) VALUES('SAIF', 'account')")
            conn.execute("INSERT INTO entities(name, entity_type) VALUES('AI', 'concept')")
            conn.execute(
                """INSERT INTO facts(content, fact_type, status)
                   VALUES('SAIF approved the pilot, not SAIFER.', 'finding', 'active')"""
            )
            conn.commit()
            conn.close()

            preview = run_backfill(str(db_path))
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["facts_scanned"], 1)
            self.assertEqual(preview["candidate_edges"], 1)
            self.assertEqual(preview["edges_would_be_inserted"], 1)
            self.assertEqual(preview["sample"][0]["entity_name"], "SAIF")

            applied = run_backfill(str(db_path), apply=True, backup_dir=directory)
            self.assertFalse(applied["dry_run"])
            self.assertEqual(applied["edges_inserted"], 1)
            self.assertTrue(applied["backup"]["ok"])
            self.assertIsNotNone(applied["rollback_stamp"])

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT COUNT(*) FROM entity_mentions WHERE parent_type='fact'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(rows, 1)


if __name__ == "__main__":
    unittest.main()
