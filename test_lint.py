#!/usr/bin/env python3
"""
test_lint.py — Tests for ltm_lint.py health checks.

Creates temporary memory databases with specific conditions and verifies
each check reports correctly.
"""

import os
import sqlite3
import tempfile
import shutil
import unittest
import datetime

from ltm_lint import run_lint

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "init-memory.sql")


def _days_ago(n):
    """Return an ISO timestamp n days in the past."""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _init_db(db_path):
    """Create a clean database from schema."""
    conn = sqlite3.connect(db_path)
    conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
    return conn


class TestLintHealthy(unittest.TestCase):
    """A clean database with recent data should report healthy."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('healthy-topic', 'Healthy Topic', 'research', 'active', ?)",
            (_days_ago(1),),
        )
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, "
            "last_accessed_at, created_at) "
            "VALUES (1, 'Recent fact', 'insight', 3, 'active', ?, ?)",
            (_days_ago(1), _days_ago(5)),
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, notes) "
            "VALUES ('Alice', 'person', 'Test person')"
        )
        conn.execute(
            "INSERT INTO entity_mentions (entity_id, parent_id, parent_type, relationship) "
            "VALUES (1, 1, 'topic', 'about')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_healthy_db(self):
        result = run_lint({"db_path": self.db_path})
        self.assertTrue(result["healthy"])
        self.assertEqual(result["checks"]["stale_facts"]["count"], 0)
        self.assertEqual(result["checks"]["stale_topics"]["count"], 0)
        self.assertEqual(result["checks"]["orphaned_entities"]["count"], 0)
        self.assertEqual(result["checks"]["empty_topics"]["count"], 0)
        self.assertEqual(result["checks"]["contradictions"]["count"], 0)
        self.assertEqual(result["checks"]["superseded_in_active"]["count"], 0)
        self.assertEqual(result["checks"]["unresolved_todos"]["count"], 0)
        self.assertTrue(result["checks"]["fts_sync"]["in_sync"])
        self.assertEqual(result["summary"], "all checks passed")


class TestStaleFacts(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('topic-a', 'Topic A', 'research', 'active', ?)",
            (_days_ago(1),),
        )
        # Stale fact: last accessed 90 days ago, importance >= 3
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, last_accessed_at) "
            "VALUES (1, 'Old important fact that nobody reads anymore', 'insight', 4, 'active', ?)",
            (_days_ago(90),),
        )
        # Stale fact with NULL last_accessed_at (created 90 days ago)
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, created_at) "
            "VALUES (1, 'Never-accessed fact from long ago', 'finding', 3, 'active', ?)",
            (_days_ago(90),),
        )
        # Fresh fact — should NOT appear
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, last_accessed_at) "
            "VALUES (1, 'Recent fact', 'insight', 5, 'active', ?)",
            (_days_ago(2),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stale_facts_detected(self):
        result = run_lint({"db_path": self.db_path})
        stale = result["checks"]["stale_facts"]
        self.assertEqual(stale["count"], 2)
        contents = [i["content"] for i in stale["items"]]
        self.assertTrue(any("Old important fact" in c for c in contents))
        self.assertTrue(any("Never-accessed fact" in c for c in contents))

    def test_stale_facts_have_days(self):
        result = run_lint({"db_path": self.db_path})
        for item in result["checks"]["stale_facts"]["items"]:
            self.assertIn("days_stale", item)
            self.assertGreaterEqual(item["days_stale"], 60)


class TestStaleTopics(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        # Stale topic (100 days old)
        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('stale-topic', 'Stale Topic', 'research', 'active', ?)",
            (_days_ago(100),),
        )
        # Fresh topic
        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('fresh-topic', 'Fresh Topic', 'research', 'active', ?)",
            (_days_ago(5),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stale_topics_detected(self):
        result = run_lint({"db_path": self.db_path})
        stale = result["checks"]["stale_topics"]
        self.assertEqual(stale["count"], 1)
        self.assertEqual(stale["items"][0]["slug"], "stale-topic")
        self.assertGreaterEqual(stale["items"][0]["days_stale"], 90)


class TestOrphanedEntities(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('t1', 'Topic 1', 'research', 'active', ?)", (_days_ago(1),)
        )
        # Linked entity
        conn.execute(
            "INSERT INTO entities (name, entity_type) VALUES ('Linked Person', 'person')"
        )
        conn.execute(
            "INSERT INTO entity_mentions (entity_id, parent_id, parent_type, relationship) "
            "VALUES (1, 1, 'topic', 'about')"
        )
        # Orphaned entity — no mentions
        conn.execute(
            "INSERT INTO entities (name, entity_type) VALUES ('Orphaned Tool', 'tool')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_orphaned_entities_detected(self):
        result = run_lint({"db_path": self.db_path})
        orphans = result["checks"]["orphaned_entities"]
        self.assertEqual(orphans["count"], 1)
        self.assertEqual(orphans["items"][0]["name"], "Orphaned Tool")
        self.assertEqual(orphans["items"][0]["entity_type"], "tool")


class TestEmptyTopics(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        # Topic with facts
        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('has-facts', 'Has Facts', 'research', 'active', ?)", (_days_ago(1),)
        )
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, last_accessed_at) "
            "VALUES (1, 'A fact', 'insight', 3, 'active', ?)", (_days_ago(1),)
        )
        # Empty topic — no facts
        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('empty-topic', 'Empty Topic', 'technical', 'active', ?)",
            (_days_ago(1),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_empty_topics_detected(self):
        result = run_lint({"db_path": self.db_path})
        empty = result["checks"]["empty_topics"]
        self.assertEqual(empty["count"], 1)
        self.assertEqual(empty["items"][0]["slug"], "empty-topic")
        self.assertEqual(empty["items"][0]["category"], "technical")


class TestContradictions(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('decisions', 'Decisions', 'technical', 'active', ?)", (_days_ago(1),)
        )
        # Contradicting decision pair: "use X" vs "don't use X"
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, last_accessed_at) "
            "VALUES (1, 'Use Redis for caching', 'decision', 'active', ?)", (_days_ago(1),)
        )
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, last_accessed_at) "
            "VALUES (1, %s, 'decision', 'active', ?)" % "'Don''t use Redis — too expensive'",
            (_days_ago(1),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_contradictions_detected(self):
        result = run_lint({"db_path": self.db_path})
        contras = result["checks"]["contradictions"]
        self.assertGreaterEqual(contras["count"], 1)
        self.assertEqual(contras["items"][0]["topic_slug"], "decisions")


class TestSupersededInActive(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('active-topic', 'Active', 'research', 'active', ?)", (_days_ago(1),)
        )
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, last_accessed_at) "
            "VALUES (1, 'Superseded fact still here', 'finding', 'superseded', ?)",
            (_days_ago(1),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_superseded_detected(self):
        result = run_lint({"db_path": self.db_path})
        sup = result["checks"]["superseded_in_active"]
        self.assertEqual(sup["count"], 1)
        self.assertIn("Superseded fact", sup["items"][0]["content"])


class TestUnresolvedTodos(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('work', 'Work Items', 'project', 'active', ?)", (_days_ago(1),)
        )
        # Old todo (20 days) — should be flagged
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, created_at, last_accessed_at) "
            "VALUES (1, 'Old unresolved todo', 'todo', 'active', ?, ?)",
            (_days_ago(20), _days_ago(1)),
        )
        # Recent todo (3 days) — should NOT be flagged
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, created_at, last_accessed_at) "
            "VALUES (1, 'Fresh todo', 'todo', 'active', ?, ?)",
            (_days_ago(3), _days_ago(1)),
        )
        # Resolved todo — should NOT be flagged
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, status, created_at, last_accessed_at) "
            "VALUES (1, 'Done todo', 'todo', 'resolved', ?, ?)",
            (_days_ago(30), _days_ago(1)),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_unresolved_todos_detected(self):
        result = run_lint({"db_path": self.db_path})
        todos = result["checks"]["unresolved_todos"]
        self.assertEqual(todos["count"], 1)
        self.assertIn("Old unresolved todo", todos["items"][0]["content"])
        self.assertGreaterEqual(todos["items"][0]["days_old"], 14)
        self.assertEqual(todos["items"][0]["topic_slug"], "work")


class TestFtsSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('t1', 'T1', 'research', 'active', ?)", (_days_ago(1),)
        )
        conn.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status, last_accessed_at) "
            "VALUES (1, 'F1', 'insight', 3, 'active', ?)", (_days_ago(1),)
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fts_in_sync(self):
        result = run_lint({"db_path": self.db_path})
        fts = result["checks"]["fts_sync"]
        self.assertTrue(fts["in_sync"])
        self.assertEqual(fts["expected"], fts["actual"])

    def test_fts_drift_detected(self):
        # Manually insert an extra FTS row without a source
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO memory_fts(content, row_id, table_name) "
            "VALUES ('ghost row', 9999, 'facts')"
        )
        conn.commit()
        conn.close()

        result = run_lint({"db_path": self.db_path})
        fts = result["checks"]["fts_sync"]
        self.assertFalse(fts["in_sync"])
        self.assertGreater(fts["actual"], fts["expected"])


class TestMissingDb(unittest.TestCase):
    def test_missing_db_returns_error(self):
        result = run_lint({"db_path": "/nonexistent/path/memory.db"})
        self.assertIn("error", result)


class TestEmptyDb(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        _init_db(self.db_path).close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_empty_db_is_healthy(self):
        result = run_lint({"db_path": self.db_path})
        self.assertTrue(result["healthy"])
        self.assertEqual(result["summary"], "all checks passed")


class TestSummaryFormat(unittest.TestCase):
    """Verify the summary string aggregates issues correctly."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_lint_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        conn = _init_db(self.db_path)

        # Stale topic
        conn.execute(
            "INSERT INTO topics (slug, title, category, status, last_accessed_at) "
            "VALUES ('old', 'Old Topic', 'research', 'active', ?)", (_days_ago(120),)
        )
        # Orphaned entity
        conn.execute(
            "INSERT INTO entities (name, entity_type) VALUES ('Ghost', 'person')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_summary_contains_all_issues(self):
        result = run_lint({"db_path": self.db_path})
        self.assertFalse(result["healthy"])
        self.assertIn("stale topics", result["summary"])
        self.assertIn("orphaned entities", result["summary"])
        self.assertIn("empty topics", result["summary"])


if __name__ == "__main__":
    unittest.main()
