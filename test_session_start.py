#!/usr/bin/env python3
"""Tests for ltm_session_start.py — uses temp DBs to avoid touching real data."""

import os
import shutil
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ltm_session_start import generate_instructions

INIT_SQL = (Path(__file__).parent / "init-memory.sql").read_text(encoding="utf-8")

# ── schema for ltm_patterns / ltm_session_log (created by session-end hook) ──
LTM_EXTRA_SQL = """
CREATE TABLE IF NOT EXISTS ltm_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    pattern_type TEXT NOT NULL, description TEXT, repository TEXT, cwd TEXT,
    created_at TEXT DEFAULT (datetime('now')), UNIQUE(session_id, pattern_type));
CREATE TABLE IF NOT EXISTS ltm_session_log (
    session_id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, summary TEXT,
    end_reason TEXT, facts_extracted INTEGER DEFAULT 0,
    entities_extracted INTEGER DEFAULT 0, patterns_detected TEXT,
    processed_at TEXT DEFAULT (datetime('now')));
"""


def _make_memory_db(path: str, *, seed: bool = True) -> None:
    """Create a memory.db with full LTM schema and optional sample data."""
    conn = sqlite3.connect(path)
    conn.executescript(INIT_SQL)
    conn.executescript(LTM_EXTRA_SQL)
    if seed:
        _seed_data(conn)
    conn.commit()
    conn.close()


def _seed_data(conn: sqlite3.Connection) -> None:
    """Insert sample data covering all sections."""
    # Identity (self entity)
    conn.execute(
        "INSERT INTO entities(name, entity_type, is_self, attributes, notes) "
        "VALUES(?, ?, 1, ?, ?)",
        ("Vijay Cinn", "person", '{"role":"SE","org":"Microsoft"}', "Solutions Engineer in US East"),
    )
    # Other entities
    conn.execute(
        "INSERT INTO entities(name, entity_type, notes) VALUES(?, ?, ?)",
        ("Sarah Johnson", "person", "SSP partner"),
    )
    conn.execute(
        "INSERT INTO entities(name, entity_type, notes) VALUES(?, ?, ?)",
        ("Contoso", "account", "Enterprise customer"),
    )
    conn.execute(
        "INSERT INTO entities(name, entity_type, notes) VALUES(?, ?, ?)",
        ("Fabrikam", "account", "Mid-market customer"),
    )
    # Entity mentions
    conn.execute(
        "INSERT INTO entity_mentions(entity_id, parent_id, parent_type, relationship) "
        "VALUES(2, 1, 'topic', 'mentions')"
    )
    conn.execute(
        "INSERT INTO entity_mentions(entity_id, parent_id, parent_type, relationship) "
        "VALUES(3, 1, 'topic', 'about')"
    )

    # Topics
    conn.execute(
        "INSERT INTO topics(slug, title, category, description, status, last_accessed_at) "
        "VALUES(?, ?, ?, ?, 'active', datetime('now'))",
        ("gcc-gaps", "GCC Product Gaps", "technical", "Tracking M365/Azure gaps in GCC"),
    )
    conn.execute(
        "INSERT INTO topics(slug, title, category, status, last_accessed_at) "
        "VALUES(?, ?, ?, 'active', datetime('now', '-1 day'))",
        ("contoso-migration", "Contoso Migration", "customer"),
    )
    conn.execute(
        "INSERT INTO topics(slug, title, category, status, last_accessed_at) "
        "VALUES(?, ?, ?, 'active', datetime('now', '-2 days'))",
        ("copilot-customization", "Copilot Customization", "project"),
    )

    # High-priority facts (importance >= 4)
    conn.execute(
        "INSERT INTO facts(topic_id, content, fact_type, importance, source, status) "
        "VALUES(1, 'GCC High lacks Azure OpenAI parity', 'insight', 5, 'customer call', 'active')"
    )
    conn.execute(
        "INSERT INTO facts(topic_id, content, fact_type, importance, source, status) "
        "VALUES(1, 'FedRAMP authorization expected Q4 FY26', 'finding', 4, 'roadmap review', 'active')"
    )
    conn.execute(
        "INSERT INTO facts(topic_id, content, fact_type, importance, source, status) "
        "VALUES(2, 'Contoso wants to migrate 500 VMs by March', 'action', 4, 'email', 'active')"
    )

    # Low-priority fact (should NOT appear)
    conn.execute(
        "INSERT INTO facts(topic_id, content, fact_type, importance, status) "
        "VALUES(3, 'Copilot extensions are in preview', 'insight', 2, 'active')"
    )

    # Pending work (todo facts)
    conn.execute(
        "INSERT INTO facts(content, fact_type, importance, status) "
        "VALUES('Pending: update the Contoso ADS deck with latest pricing', 'todo', 4, 'active')"
    )
    conn.execute(
        "INSERT INTO facts(content, fact_type, importance, status) "
        "VALUES('Pending: follow up on Fabrikam Copilot POC results', 'todo', 4, 'active')"
    )

    # Stale fact (last_accessed_at > 30 days ago, importance >= 3)
    conn.execute(
        "INSERT INTO facts(content, fact_type, importance, status, last_accessed_at) "
        "VALUES('Old insight about Azure pricing tier', 'insight', 3, 'active', datetime('now', '-45 days'))"
    )

    # Patterns
    conn.execute(
        "INSERT INTO ltm_patterns(session_id, pattern_type, description) "
        "VALUES('sess-a', 'customer-engagement', 'Customer call prep')"
    )
    conn.execute(
        "INSERT INTO ltm_patterns(session_id, pattern_type, description) "
        "VALUES('sess-b', 'customer-engagement', 'Account review')"
    )
    conn.execute(
        "INSERT INTO ltm_patterns(session_id, pattern_type, description) "
        "VALUES('sess-c', 'email-drafting', 'SSP outreach')"
    )
    conn.execute(
        "INSERT INTO ltm_patterns(session_id, pattern_type, description) "
        "VALUES('sess-d', 'msx-activity', 'Pipeline review')"
    )


def _make_store_db(path: str) -> None:
    """Create a minimal session-store.db with sample sessions."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, "
        "branch TEXT, summary TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, "
        "user_message TEXT, assistant_response TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES(?, ?, ?, ?, ?, ?)",
        ("s1", "C:\\workspace\\agency", "vcinnakonda/agency", "main",
         "Built LTM session-end hook", "2025-07-01T10:00:00"),
    )
    conn.execute(
        "INSERT INTO turns VALUES(?, 0, ?, ?)",
        ("s1", "Create the session-end hook for long-term memory", "Starting implementation..."),
    )
    conn.execute(
        "INSERT INTO sessions VALUES(?, ?, ?, ?, ?, ?)",
        ("s2", "C:\\workspace\\agency", "vcinnakonda/agency", "ltm-hooks",
         "Tested memory driver", "2025-07-01T09:00:00"),
    )
    conn.execute(
        "INSERT INTO turns VALUES(?, 0, ?, ?)",
        ("s2", "Run the memory driver tests", "All tests passed."),
    )
    conn.commit()
    conn.close()


class TestSessionStart(unittest.TestCase):
    """Integration tests for the LTM session-start hook."""

    def setUp(self):
        self.test_dir = os.path.join(
            os.path.dirname(__file__), f"_test_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(self.test_dir, exist_ok=True)
        self.memory_path = os.path.join(self.test_dir, "memory.db")
        self.store_path = os.path.join(self.test_dir, "session-store.db")
        self.output_path = Path(self.test_dir) / "ltm.instructions.md"

    def tearDown(self):
        import gc, time
        gc.collect()
        time.sleep(0.1)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ── Test: full output with all sections ───────────────────────────

    def test_full_output_all_sections(self):
        _make_memory_db(self.memory_path)
        _make_store_db(self.store_path)
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            store_override=Path(self.store_path),
            output_path=self.output_path,
        )
        self.assertIsNotNone(content)
        self.assertTrue(self.output_path.exists())

        text = self.output_path.read_text(encoding="utf-8")
        # LTM markers present
        self.assertIn("<!-- LTM-START -->", text)
        self.assertIn("<!-- LTM-END -->", text)
        # Section 1: Identity
        self.assertIn("Identity", text)
        self.assertIn("Vijay Cinn", text)
        # Section 2: Active Topics
        self.assertIn("Active Topics", text)
        self.assertIn("gcc-gaps", text)
        # Section 3: High-Priority Facts
        self.assertIn("High-Priority Facts", text)
        self.assertIn("GCC High lacks Azure OpenAI parity", text)
        self.assertIn("FedRAMP", text)
        # Section 4: Pending Work
        self.assertIn("Pending Work", text)
        self.assertIn("- [ ]", text)
        self.assertIn("Contoso ADS deck", text)
        # Section 5: Recent Patterns
        self.assertIn("Recent Patterns", text)
        self.assertIn("customer-engagement", text)
        # Section 6: Recent Session Context
        self.assertIn("Recent Session Context", text)
        self.assertIn("LTM session-end hook", text)
        # Section 7: Known Entities
        self.assertIn("Known Entities", text)
        self.assertIn("Sarah Johnson", text)
        # Section 8: Staleness Nudge
        self.assertIn("30+ days", text)

    # ── Test: missing memory.db ───────────────────────────────────────

    def test_missing_memory_db(self):
        fake_path = os.path.join(self.test_dir, "nonexistent.db")
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=fake_path,
            output_path=self.output_path,
        )
        self.assertIn("not found", content)
        self.assertTrue(self.output_path.exists())

    # ── Test: empty memory.db ─────────────────────────────────────────

    def test_empty_memory_db(self):
        _make_memory_db(self.memory_path, seed=False)
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            output_path=self.output_path,
        )
        self.assertIn("empty", content.lower())

    # ── Test: no session store available ──────────────────────────────

    def test_no_session_store(self):
        """Session context section should be empty when store doesn't exist."""
        _make_memory_db(self.memory_path)
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            store_override=Path(os.path.join(self.test_dir, "noexist.db")),
            output_path=self.output_path,
        )
        self.assertIsNotNone(content)
        # Should still have other sections
        self.assertIn("Identity", content)
        # But no session context section
        self.assertNotIn("Recent Session Context", content)

    # ── Test: preserves existing content in copilot-instructions.md ───

    def test_preserves_existing_content(self):
        _make_memory_db(self.memory_path)
        # Pre-populate the output file with existing instructions
        self.output_path.write_text("# WorkIQ preferences\nUse WorkIQ first.\n", encoding="utf-8")
        generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            output_path=self.output_path,
        )
        text = self.output_path.read_text(encoding="utf-8")
        # Original content preserved
        self.assertIn("WorkIQ preferences", text)
        self.assertIn("Use WorkIQ first.", text)
        # LTM content appended
        self.assertIn("<!-- LTM-START -->", text)
        self.assertIn("Identity", text)

    # ── Test: idempotent (multiple runs produce valid output) ─────────

    def test_idempotent(self):
        _make_memory_db(self.memory_path)
        hook = {"cwd": "C:\\workspace\\agency"}
        c1 = generate_instructions(hook, memory_db=self.memory_path, output_path=self.output_path)
        c2 = generate_instructions(hook, memory_db=self.memory_path, output_path=self.output_path)
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        # Both should produce valid instructions (timestamps may differ)
        self.assertIn("Identity", c1)
        self.assertIn("Identity", c2)
        # File should contain exactly ONE LTM block (not duplicated)
        text = self.output_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- LTM-START -->"), 1)
        self.assertEqual(text.count("<!-- LTM-END -->"), 1)

    # ── Test: output stays concise ────────────────────────────────────

    def test_output_concise(self):
        _make_memory_db(self.memory_path)
        _make_store_db(self.store_path)
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            store_override=Path(self.store_path),
            output_path=self.output_path,
        )
        line_count = len(content.split("\n"))
        self.assertLessEqual(line_count, 150, f"Output too long: {line_count} lines")

    # ── Test: low-priority facts excluded ─────────────────────────────

    def test_low_priority_excluded(self):
        _make_memory_db(self.memory_path)
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            output_path=self.output_path,
        )
        self.assertNotIn("Copilot extensions are in preview", content)

    # ── Test: identity not shown when is_self missing ─────────────────

    def test_no_self_entity(self):
        _make_memory_db(self.memory_path, seed=False)
        conn = sqlite3.connect(self.memory_path)
        conn.execute(
            "INSERT INTO topics(slug, title, category, status, last_accessed_at) "
            "VALUES('test', 'Test Topic', 'research', 'active', datetime('now'))"
        )
        conn.commit()
        conn.close()
        content = generate_instructions(
            {"cwd": "C:\\workspace\\agency"},
            memory_db=self.memory_path,
            output_path=self.output_path,
        )
        self.assertNotIn("Identity", content)
        self.assertIn("Active Topics", content)


if __name__ == "__main__":
    unittest.main()
