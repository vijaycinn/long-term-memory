#!/usr/bin/env python3
"""
test_memory_driver.py — Regression tests for memory_driver.py.

Each test class maps to a defect observed in real agent sessions:

  TestFtsOperatorTerms      hyphenated queries crashed FTS5
  TestSearchResultShape     callers used .content and got None
  TestFactRecency           new facts were invisible to export_context
  TestExportFairness        one hot topic starved every other topic
  TestExportSections        entities / snapshots / pending never rendered
  TestSetIdentity           identity was an undiscoverable entities.is_self row
  TestMigrate               pre-existing NULL last_accessed_at rows stayed hidden
"""

import os
import sqlite3
import tempfile
import shutil
import unittest

import memory_driver as md

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "init-memory.sql")


class DriverTestCase(unittest.TestCase):
    """Builds an isolated DB + context file per test."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ltm_driver_test_")
        self.db_path = os.path.join(self.test_dir, "memory.db")
        self.ctx_path = os.path.join(self.test_dir, "memory-context.md")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # helpers -----------------------------------------------------------
    def base(self, **kw):
        kw.setdefault("db_path", self.db_path)
        kw.setdefault("context_path", self.ctx_path)
        return kw

    def topic(self, slug, title=None, category="research"):
        return md.add_topic(self.base(slug=slug, title=title or slug, category=category))["topic_id"]

    def fact(self, content, topic_id=None, importance=4, fact_type="finding"):
        return md.add_fact(self.base(content=content, topic_id=topic_id,
                                     importance=importance, fact_type=fact_type))

    def raw(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestFtsOperatorTerms(DriverTestCase):
    """'-' and ':' are FTS5 operators; bare terms containing them crashed the driver."""

    def setUp(self):
        super().setUp()
        t = self.topic("prefs")
        self.fact("Bare remember-this means run the memory pipeline autonomously", t)
        self.fact("The cx-followup skill drafts customer email", t)
        self.fact("Foundry and Anthropic both land in Frontier", t)

    def test_hyphenated_term_does_not_raise(self):
        # Regression: previously OperationalError "no such column: this"
        rows = md.search_memory(self.base(query="remember-this"))
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(any("remember-this" in r["text"] for r in rows))

    def test_hyphenated_term_flags_sanitized(self):
        rows = md.search_memory(self.base(query="cx-followup"))
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(rows[0]["sanitized"])

    def test_plain_term_is_not_sanitized(self):
        rows = md.search_memory(self.base(query="Foundry"))
        self.assertGreaterEqual(len(rows), 1)
        self.assertFalse(rows[0]["sanitized"])

    def test_boolean_syntax_still_works(self):
        # No regression: power-user FTS5 syntax must survive untouched.
        rows = md.search_memory(self.base(query="Foundry OR Anthropic"))
        self.assertGreaterEqual(len(rows), 1)
        self.assertFalse(rows[0]["sanitized"])

    def test_raw_mode_surfaces_the_error(self):
        with self.assertRaises(sqlite3.OperationalError):
            md.search_memory(self.base(query="remember-this", raw=True))

    def test_sanitizer_preserves_boolean_keywords(self):
        self.assertEqual(md._fts_sanitize("a-b OR c-d"), '"a-b" OR "c-d"')

    def test_sanitizer_escapes_embedded_quotes(self):
        self.assertEqual(md._fts_quote('say "hi"'), '"say ""hi"""')


class TestSearchResultShape(DriverTestCase):
    """Callers reached for .content and got None because only .text existed."""

    def test_text_and_content_are_both_populated(self):
        t = self.topic("shape")
        self.fact("Databases bucket is twenty percent of the plan", t)
        rows = md.search_memory(self.base(query="Databases"))
        self.assertGreaterEqual(len(rows), 1)
        r = rows[0]
        self.assertTrue(r["text"])
        self.assertEqual(r["content"], r["text"])


class TestFactRecency(DriverTestCase):
    """A fact inserted with NULL last_accessed_at sorts last and never surfaces."""

    def test_add_fact_sets_last_accessed(self):
        t = self.topic("recency")
        fid = self.fact("brand new fact", t)["fact_id"]
        row = self.raw("SELECT last_accessed_at FROM facts WHERE id=?", (fid,))[0]
        self.assertIsNotNone(row[0])

    def test_add_topic_sets_last_accessed(self):
        self.topic("fresh-topic")
        row = self.raw("SELECT last_accessed_at FROM topics WHERE slug=?", ("fresh-topic",))[0]
        self.assertIsNotNone(row[0])

    def test_reasserting_a_fact_refreshes_recency(self):
        t = self.topic("recency")
        first = self.fact("duplicate content", t)
        self.raw_update("UPDATE facts SET last_accessed_at='2000-01-01T00:00:00Z' WHERE id=?",
                        (first["fact_id"],))
        again = self.fact("duplicate content", t)
        self.assertFalse(again["inserted"])
        self.assertEqual(again["fact_id"], first["fact_id"])
        row = self.raw("SELECT last_accessed_at FROM facts WHERE id=?", (first["fact_id"],))[0]
        self.assertNotEqual(row[0], "2000-01-01T00:00:00Z")

    def test_new_fact_reaches_export_without_manual_touch(self):
        t = self.topic("recency")
        self.fact("must appear in the bridge immediately", t, importance=5)
        md.export_context(self.base())
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertIn("must appear in the bridge immediately", body)

    def raw_update(self, sql, args):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()


class TestExportFairness(DriverTestCase):
    """A single hot topic used to consume the whole global fact budget."""

    def setUp(self):
        super().setUp()
        self.hot = self.topic("hot-topic")
        self.quiet = self.topic("quiet-topic")
        for i in range(30):
            self.fact(f"hot fact number {i}", self.hot, importance=5)
        self.fact("quiet but important fact", self.quiet, importance=4)

    def test_quiet_topic_still_represented(self):
        md.export_context(self.base(topic_limit=5, per_topic_facts=6, max_facts=40))
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertIn("quiet but important fact", body)

    def test_hot_topic_is_capped_by_quota(self):
        res = md.export_context(self.base(topic_limit=5, per_topic_facts=6, max_facts=12))
        self.assertLessEqual(res["facts_loaded"], 12)
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertLessEqual(body.count("hot fact number"), 12)

    def test_importance_four_survives_many_importance_five(self):
        # Previously: 30 importance-5 facts filled LIMIT 20 and the
        # importance-4 fact became permanently unreachable.
        md.export_context(self.base())
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertIn("quiet but important fact", body)

    def test_limits_are_tunable(self):
        small = md.export_context(self.base(per_topic_facts=1, max_facts=2))
        self.assertLessEqual(small["facts_loaded"], 2)
        big = md.export_context(self.base(per_topic_facts=10, max_facts=40))
        self.assertGreater(big["facts_loaded"], small["facts_loaded"])

    def test_no_duplicate_facts_across_pools(self):
        md.export_context(self.base())
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertEqual(body.count("quiet but important fact"), 1)


class TestExportSections(DriverTestCase):
    """Entities, snapshots and pending work were stored but never rendered."""

    def setUp(self):
        super().setUp()
        self.t = self.topic("sections")
        self.fact("a high priority finding", self.t, importance=5)
        self.fact("chase the Data SE pairing", self.t, importance=5, fact_type="todo")
        md.add_entity(self.base(name="Five9, Inc.", entity_type="account",
                                notes="SDP T1, Arc SQL signal"))
        md.set_identity(self.base(name="Test User", notes="Sr DSE"))
        md.add_snapshot(self.base(topic_id=self.t, title="Session One",
                                  next_steps="confirm the bucket variant",
                                  open_questions="which Databases variant?"))
        md.export_context(self.base())
        self.body = open(self.ctx_path, encoding="utf-8").read()

    def test_identity_rendered(self):
        self.assertIn("Test User", self.body)

    def test_entities_section_rendered(self):
        self.assertIn("Known Entities", self.body)
        self.assertIn("Five9, Inc.", self.body)

    def test_self_entity_excluded_from_entity_list(self):
        entities_block = self.body.split("Known Entities")[1]
        self.assertNotIn("Test User", entities_block)

    def test_snapshot_next_steps_rendered(self):
        self.assertIn("Where I Left Off", self.body)
        self.assertIn("confirm the bucket variant", self.body)

    def test_pending_work_rendered(self):
        self.assertIn("Pending Work", self.body)
        self.assertIn("chase the Data SE pairing", self.body)

    def test_counts_returned(self):
        res = md.export_context(self.base())
        for key in ("topics_loaded", "facts_loaded", "snapshots_loaded",
                    "pending_loaded", "entities_loaded"):
            self.assertIn(key, res)


class TestSetIdentity(DriverTestCase):
    """There is no identity table; identity is entities.is_self and must be unique."""

    def test_creates_self_entity(self):
        md.set_identity(self.base(name="Vijay", notes="Sr DSE"))
        rows = self.raw("SELECT name FROM entities WHERE is_self=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Vijay")

    def test_upserts_in_place_without_duplicating(self):
        first = md.set_identity(self.base(name="Vijay", notes="FY26"))
        second = md.set_identity(self.base(name="Vijay", notes="FY27"))
        self.assertEqual(first["entity_id"], second["entity_id"])
        self.assertEqual(len(self.raw("SELECT id FROM entities WHERE name='Vijay'")), 1)

    def test_only_one_self_row_survives(self):
        md.set_identity(self.base(name="Old Self"))
        md.set_identity(self.base(name="New Self"))
        rows = self.raw("SELECT name FROM entities WHERE is_self=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "New Self")

    def test_notes_are_updated(self):
        md.set_identity(self.base(name="Vijay", notes="FY26 248 accounts"))
        md.set_identity(self.base(name="Vijay", notes="FY27 393 accounts"))
        rows = self.raw("SELECT notes FROM entities WHERE is_self=1")
        self.assertIn("FY27", rows[0][0])


class TestMigrate(DriverTestCase):
    """Rows written by the old driver kept NULL last_accessed_at and stayed hidden."""

    def test_backfills_null_last_accessed(self):
        t = self.topic("legacy")
        fid = self.fact("legacy fact", t, importance=5)["fact_id"]
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE facts SET last_accessed_at=NULL WHERE id=?", (fid,))
        conn.execute("UPDATE topics SET last_accessed_at=NULL WHERE id=?", (t,))
        conn.commit()
        conn.close()

        res = md.migrate(self.base())
        self.assertGreaterEqual(res["facts_backfilled"], 1)
        self.assertGreaterEqual(res["topics_backfilled"], 1)
        self.assertIsNotNone(
            self.raw("SELECT last_accessed_at FROM facts WHERE id=?", (fid,))[0][0]
        )

    def test_is_idempotent(self):
        self.topic("legacy")
        md.migrate(self.base())
        second = md.migrate(self.base())
        self.assertEqual(second["facts_backfilled"], 0)
        self.assertEqual(second["topics_backfilled"], 0)


class TestSlugSearchable(DriverTestCase):
    """v1 indexed topics as title+description, so searching by slug found nothing."""

    def setUp(self):
        super().setUp()
        self.topic("fy27-comp-plan", title="FY27 SE Comp Plan", category="career")

    def test_topic_findable_by_slug(self):
        rows = md.search_memory(self.base(query="fy27-comp-plan"))
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["src"], "topics")

    def test_topic_still_findable_by_title(self):
        rows = md.search_memory(self.base(query="Comp"))
        self.assertGreaterEqual(len(rows), 1)

    def test_reindex_reports_rows_and_version(self):
        res = md.reindex_fts(self.base())
        self.assertGreaterEqual(res["fts_rows"], 1)
        self.assertEqual(res["schema_version"], md.SCHEMA_VERSION)

    def test_reindex_does_not_duplicate_index_rows(self):
        first = md.reindex_fts(self.base())["fts_rows"]
        second = md.reindex_fts(self.base())["fts_rows"]
        self.assertEqual(first, second)

    def test_migrate_upgrades_legacy_index(self):
        # Simulate a v1 database: legacy trigger/view plus stale version.
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS fts_topics_insert;
            DROP VIEW IF EXISTS fts_resolved;
            CREATE VIEW fts_resolved AS
              SELECT 'facts' AS src, id, content AS text, fact_type AS subtype FROM facts
              UNION ALL
              SELECT 'entities' AS src, id, name || ' ' || COALESCE(notes,'') AS text,
                     entity_type AS subtype FROM entities
              UNION ALL
              SELECT 'topics' AS src, id, title || ' ' || COALESCE(description,'') AS text,
                     category AS subtype FROM topics
              UNION ALL
              SELECT 'snapshots' AS src, id,
                     COALESCE(summary,'') || ' ' || COALESCE(findings,'') AS text,
                     NULL AS subtype FROM snapshots;
            DELETE FROM memory_fts;
            INSERT INTO memory_fts(content, row_id, table_name)
              SELECT title || ' ' || COALESCE(description,''), id, 'topics' FROM topics;
            UPDATE schema_version SET version = 1;
            """
        )
        conn.commit()
        conn.close()

        self.assertEqual(len(md.search_memory(self.base(query="fy27-comp-plan"))), 0)
        md.migrate(self.base())
        self.assertGreaterEqual(len(md.search_memory(self.base(query="fy27-comp-plan"))), 1)

    def test_migrate_is_idempotent_after_upgrade(self):
        md.migrate(self.base())
        second = md.migrate(self.base())
        self.assertIsNone(second["reindexed"])
        self.assertEqual(second["schema_version"], md.SCHEMA_VERSION)


class TestMarkdownSafety(DriverTestCase):
    """Fact bodies containing markdown used to corrupt the bridge structure."""

    def setUp(self):
        super().setUp()
        self.t = self.topic("md-safety")
        self.fact("intro line\n## Pre-flight check\nmore detail", self.t, importance=5)
        md.export_context(self.base())
        self.body = open(self.ctx_path, encoding="utf-8").read()

    def test_embedded_heading_does_not_become_a_section(self):
        headings = [l for l in self.body.splitlines() if l.startswith("## ")]
        self.assertNotIn("## Pre-flight check", headings)

    def test_content_is_preserved_on_one_line(self):
        self.assertIn("intro line ## Pre-flight check more detail", self.body)

    def test_only_known_sections_present(self):
        allowed = {"Identity", "Active Topics", "High-Priority Facts",
                   "Where I Left Off", "Pending Work", "Known Entities"}
        for line in self.body.splitlines():
            if line.startswith("## "):
                self.assertTrue(
                    any(a in line for a in allowed), f"unexpected section: {line}"
                )

    def test_flatten_helper(self):
        self.assertEqual(md._md_inline("a\n\n  b\tc "), "a b c")
        self.assertEqual(md._md_inline(None), "")


class TestBatch(DriverTestCase):
    """Process startup dominates latency; batching must collapse it safely."""

    def test_runs_all_ops_in_order(self):
        res = md.batch(self.base(ops=[
            {"op": "add_topic", "slug": "b1", "title": "B1", "category": "research"},
            {"op": "add_fact", "content": "batched fact", "topic_slug": "b1",
             "importance": 5},
            {"op": "export_context"},
        ]))
        self.assertEqual(len(res["results"]), 3)
        self.assertIn("topic_id", res["results"][0])
        self.assertTrue(res["results"][1]["inserted"])
        self.assertIn("path", res["results"][2])

    def test_topic_slug_is_resolved(self):
        # Regression: passing topic_slug used to silently create a GLOBAL fact.
        md.batch(self.base(ops=[
            {"op": "add_topic", "slug": "b2", "title": "B2", "category": "research"},
            {"op": "add_fact", "content": "attached fact", "topic_slug": "b2"},
        ]))
        rows = self.raw(
            "SELECT t.slug FROM facts f JOIN topics t ON t.id=f.topic_id "
            "WHERE f.content='attached fact'")
        self.assertEqual(rows[0][0], "b2")

    def test_inherits_db_path(self):
        res = md.batch(self.base(ops=[{"op": "get_stats"}]))
        self.assertIn("facts", res["results"][0])

    def test_one_failure_does_not_abort_the_batch(self):
        res = md.batch(self.base(ops=[
            {"op": "nonexistent_op"},
            {"op": "add_topic", "slug": "b3", "title": "B3", "category": "research"},
        ]))
        self.assertIn("error", res["results"][0])
        self.assertIn("topic_id", res["results"][1])

    def test_batched_fact_reaches_the_bridge(self):
        md.batch(self.base(ops=[
            {"op": "add_topic", "slug": "b4", "title": "B4", "category": "research"},
            {"op": "add_fact", "content": "end to end batched", "topic_slug": "b4",
             "importance": 5},
            {"op": "export_context"},
        ]))
        body = open(self.ctx_path, encoding="utf-8").read()
        self.assertIn("end to end batched", body)

    def test_add_snapshot_accepts_topic_slug(self):
        # Regression: add_snapshot required topic_id and raised
        # KeyError('topic_id') on topic_slug, silently losing the snapshot.
        md.batch(self.base(ops=[
            {"op": "add_topic", "slug": "b5", "title": "B5", "category": "research"},
        ]))
        res = md.add_snapshot(self.base(topic_slug="b5", title="Slug snapshot",
                                        next_steps="carry on"))
        self.assertEqual(res["seq_number"], 1)
        rows = self.raw(
            "SELECT s.title FROM snapshots s JOIN topics t ON t.id=s.topic_id "
            "WHERE t.slug='b5'")
        self.assertEqual(rows[0][0], "Slug snapshot")

    def test_add_snapshot_reports_unknown_slug(self):
        res = md.add_snapshot(self.base(topic_slug="does-not-exist", title="x"))
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
