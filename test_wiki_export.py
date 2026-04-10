#!/usr/bin/env python3
"""
test_wiki_export.py — Tests for ltm_wiki_export.py

Creates a temporary memory.db with sample data, runs the wiki export,
and verifies all expected files and content are generated.
"""

import json
import os
import sqlite3
import tempfile
import shutil
import unittest

from ltm_wiki_export import export_wiki


SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "init-memory.sql")


def create_test_db(db_path):
    """Create a memory.db with realistic sample data."""
    conn = sqlite3.connect(db_path)
    conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())

    # Topics
    conn.execute(
        "INSERT INTO topics (slug, title, category, description, status, last_accessed_at) "
        "VALUES ('saif-cu', 'SAIF CU Migration', 'customer', 'Credit union core migration project', 'active', '2026-04-10T12:00:00Z')"
    )
    conn.execute(
        "INSERT INTO topics (slug, title, category, description, status, last_accessed_at) "
        "VALUES ('fort-worth', 'City of Fort Worth', 'customer', 'Municipal cloud adoption', 'active', '2026-04-09T08:00:00Z')"
    )
    conn.execute(
        "INSERT INTO topics (slug, title, category, status) "
        "VALUES ('gcc-gaps', 'GCC Product Gaps', 'technical', 'active')"
    )

    # Facts for saif-cu (topic_id=1)
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, confidence, source, status) "
        "VALUES (1, 'CU migration target date is Q3 FY26', 'decision', 5, 4, 'email Oct 2025', 'active')"
    )
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, confidence, source, status) "
        "VALUES (1, 'TPS API integration blocked on vendor response', 'constraint', 4, 3, 'Teams call', 'active')"
    )
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, status) "
        "VALUES (1, 'Escalate TPS questions to CU product team', 'todo', 3, 'active')"
    )
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, status) "
        "VALUES (1, 'Initial vendor assessment complete', 'todo', 3, 'resolved')"
    )

    # Facts for fort-worth (topic_id=2)
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, status) "
        "VALUES (2, 'Prepare tenant architecture docs', 'todo', 4, 'active')"
    )
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, source, status) "
        "VALUES (2, 'Azure Gov region selected: USGov Virginia', 'decision', 4, 'workshop notes', 'active')"
    )

    # Global preference fact (no topic)
    conn.execute(
        "INSERT INTO facts (content, fact_type, importance, status) "
        "VALUES ('Prefer terse HTML reports for territory reviews', 'preference', 3, 'active')"
    )
    conn.execute(
        "INSERT INTO facts (content, fact_type, importance, status) "
        "VALUES ('Core territories are 0807, 0808, 0909, 0910, 0911', 'preference', 4, 'active')"
    )

    # A superseded fact to test strikethrough
    conn.execute(
        "INSERT INTO facts (topic_id, content, fact_type, importance, status) "
        "VALUES (3, 'GCC supports only IL2 workloads', 'finding', 3, 'superseded')"
    )

    # Entities
    conn.execute(
        "INSERT INTO entities (name, entity_type, notes) "
        "VALUES ('Ramesh Balasubramanyan', 'person', 'Developer at SAIF, primary CU technical lead')"
    )
    conn.execute(
        "INSERT INTO entities (name, entity_type, notes) "
        "VALUES ('Lee Alvarez', 'person', 'City of Fort Worth IT')"
    )
    conn.execute(
        "INSERT INTO entities (name, entity_type, notes) "
        "VALUES ('SAIF Corporation', 'account', 'Credit union holding company')"
    )
    conn.execute(
        "INSERT INTO entities (name, entity_type, notes) "
        "VALUES ('Azure DevOps', 'tool', 'CI/CD platform used by SAIF')"
    )
    conn.execute(
        "INSERT INTO entities (name, entity_type, is_self, notes) "
        "VALUES ('Vijay Cinn', 'person', 1, 'Microsoft SE')"
    )

    # Entity mentions linking to topics
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, parent_id, parent_type, relationship) "
        "VALUES (1, 1, 'topic', 'about')"  # Ramesh -> saif-cu
    )
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, parent_id, parent_type, relationship) "
        "VALUES (3, 1, 'topic', 'about')"  # SAIF Corp -> saif-cu
    )
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, parent_id, parent_type, relationship) "
        "VALUES (2, 2, 'topic', 'about')"  # Lee -> fort-worth
    )

    # Snapshot
    conn.execute(
        "INSERT INTO snapshots (topic_id, seq_number, title, summary, findings, next_steps) "
        "VALUES (1, 1, 'Initial Assessment', 'Reviewed CU migration feasibility', "
        "'Legacy COBOL systems need wrapper APIs', 'Schedule vendor call for TPS API')"
    )
    conn.execute(
        "INSERT INTO snapshots (topic_id, seq_number, title, summary, findings) "
        "VALUES (1, 2, 'Vendor Follow-up', 'TPS vendor confirmed API availability Q2', "
        "'REST API available, SOAP deprecated')"
    )

    conn.commit()
    conn.close()


class TestWikiExport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_wiki_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        self.wiki_path = os.path.join(self.test_dir, "ltm-wiki")
        create_test_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _export(self):
        return export_wiki({"db_path": self.db_path, "wiki_path": self.wiki_path})

    def _read(self, *parts):
        path = os.path.join(self.wiki_path, *parts)
        with open(path, encoding="utf-8") as f:
            return f.read()

    # ── Structure tests ───────────────────────────────────────────────

    def test_all_expected_files_created(self):
        result = self._export()
        self.assertNotIn("error", result)

        expected = [
            "index.md",
            "pending.md",
            "preferences.md",
            "log.md",
            os.path.join("topics", "saif-cu.md"),
            os.path.join("topics", "fort-worth.md"),
            os.path.join("topics", "gcc-gaps.md"),
            os.path.join("entities", "people.md"),
            os.path.join("entities", "accounts.md"),
            os.path.join("entities", "products.md"),
        ]
        for rel in expected:
            full = os.path.join(self.wiki_path, rel)
            self.assertTrue(os.path.exists(full), f"Missing: {rel}")

    def test_result_json_stats(self):
        result = self._export()
        self.assertEqual(result["topics_exported"], 3)
        self.assertEqual(result["entities_exported"], 5)
        self.assertGreater(result["facts_exported"], 0)
        self.assertIn("wiki_path", result)

    # ── Index tests ───────────────────────────────────────────────────

    def test_index_contains_topic_links(self):
        self._export()
        index = self._read("index.md")
        self.assertIn("[SAIF CU Migration](topics/saif-cu.md)", index)
        self.assertIn("[City of Fort Worth](topics/fort-worth.md)", index)
        self.assertIn("3 topics", index)

    def test_index_contains_entity_links(self):
        self._export()
        index = self._read("index.md")
        self.assertIn("[People](entities/people.md)", index)
        self.assertIn("[Accounts](entities/accounts.md)", index)

    def test_index_contains_quick_links(self):
        self._export()
        index = self._read("index.md")
        self.assertIn("[Pending Work](pending.md)", index)
        self.assertIn("[Preferences](preferences.md)", index)

    # ── Topic page tests ──────────────────────────────────────────────

    def test_topic_page_has_facts(self):
        self._export()
        page = self._read("topics", "saif-cu.md")
        self.assertIn("CU migration target date is Q3 FY26", page)
        self.assertIn("**[decision]**", page)
        self.assertIn("_Source: email Oct 2025_", page)

    def test_topic_page_has_frontmatter(self):
        self._export()
        page = self._read("topics", "saif-cu.md")
        self.assertIn("type: topic", page)
        self.assertIn("slug: saif-cu", page)
        self.assertIn("category: customer", page)

    def test_topic_page_has_snapshots(self):
        self._export()
        page = self._read("topics", "saif-cu.md")
        self.assertIn("## Snapshots", page)
        self.assertIn("Initial Assessment", page)
        self.assertIn("Vendor Follow-up", page)
        self.assertIn("Legacy COBOL systems need wrapper APIs", page)

    def test_topic_page_has_related_entities(self):
        self._export()
        page = self._read("topics", "saif-cu.md")
        self.assertIn("## Related Entities", page)
        self.assertIn("Ramesh Balasubramanyan", page)
        self.assertIn("SAIF Corporation", page)

    def test_topic_page_back_link(self):
        self._export()
        page = self._read("topics", "saif-cu.md")
        self.assertIn("[← Back to Index](../index.md)", page)

    def test_superseded_fact_strikethrough(self):
        self._export()
        page = self._read("topics", "gcc-gaps.md")
        self.assertIn("~~GCC supports only IL2 workloads~~", page)

    # ── Entity page tests ─────────────────────────────────────────────

    def test_people_page(self):
        self._export()
        page = self._read("entities", "people.md")
        self.assertIn("## Ramesh Balasubramanyan", page)
        self.assertIn("Developer at SAIF", page)
        self.assertIn("## Lee Alvarez", page)
        self.assertIn("[saif-cu](../topics/saif-cu.md)", page)

    def test_accounts_page(self):
        self._export()
        page = self._read("entities", "accounts.md")
        self.assertIn("## SAIF Corporation", page)
        self.assertIn("Credit union holding company", page)

    def test_products_page(self):
        self._export()
        page = self._read("entities", "products.md")
        self.assertIn("## Azure DevOps", page)
        self.assertIn("CI/CD platform", page)

    # ── Pending page tests ────────────────────────────────────────────

    def test_pending_page(self):
        self._export()
        page = self._read("pending.md")
        self.assertIn("[ ]", page)
        self.assertIn("[saif-cu]", page)
        self.assertIn("Escalate TPS questions", page)
        self.assertIn("[x]", page)
        self.assertIn("Initial vendor assessment complete", page)

    # ── Preferences page tests ────────────────────────────────────────

    def test_preferences_page(self):
        self._export()
        page = self._read("preferences.md")
        self.assertIn("Prefer terse HTML reports", page)
        self.assertIn("Core territories are 0807", page)

    # ── Log page tests ────────────────────────────────────────────────

    def test_log_page(self):
        self._export()
        page = self._read("log.md")
        self.assertIn("Topics exported: 3", page)
        self.assertIn("Schema version:", page)

    # ── Idempotency test ──────────────────────────────────────────────

    def test_idempotent_rerun(self):
        r1 = self._export()
        r2 = self._export()
        self.assertEqual(r1["topics_exported"], r2["topics_exported"])
        self.assertEqual(r1["facts_exported"], r2["facts_exported"])

    # ── Empty database test ───────────────────────────────────────────

    def test_empty_db(self):
        empty_db = os.path.join(self.test_dir, "empty.db")
        conn = sqlite3.connect(empty_db)
        conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
        conn.close()

        result = export_wiki({
            "db_path": empty_db,
            "wiki_path": os.path.join(self.test_dir, "empty-wiki"),
        })
        self.assertNotIn("error", result)
        self.assertEqual(result["topics_exported"], 0)
        self.assertEqual(result["entities_exported"], 0)

        wiki = os.path.join(self.test_dir, "empty-wiki")
        with open(os.path.join(wiki, "index.md"), encoding="utf-8") as f:
            idx = f.read()
        self.assertIn("0 topics", idx)
        self.assertIn("_No topics yet._", idx)

    # ── Missing DB test ───────────────────────────────────────────────

    def test_missing_db(self):
        result = export_wiki({
            "db_path": os.path.join(self.test_dir, "nonexistent.db"),
            "wiki_path": self.wiki_path,
        })
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
