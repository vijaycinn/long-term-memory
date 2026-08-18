# Repository is organised by function; nothing is installed as a package, so the
# sibling folders holding the modules under test go on the path explicitly.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sib in ("core", "hooks", "ops", "wiki"):
    _p = _os.path.join(_REPO, _sib)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
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
import json
import time
import sqlite3
import tempfile
import shutil
import unittest

import memory_driver as md

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setup", "init-memory.sql")


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

    # ── Punctuation beyond '-' and ':' ────────────────────────────────
    #
    # The original sanitiser used a DENYLIST, set('-:^*()"'). A denylist has to
    # enumerate every character FTS5 dislikes and fails silently on the first
    # one it missed. '.' was missing, so searching for a filename crashed with
    # 'fts5: syntax error near "."' instead of falling back — and the stored
    # memory is full of filenames, paths, emails and percentages.

    def test_search_survives_every_punctuation_class(self):
        """Contract, not one example — a denylist would keep missing characters."""
        cases = {
            "dot":       "ltm.instructions.md",
            "path":      r"C:\workspace\agency",
            "email":     "vcinnakonda@microsoft.com",
            "slash":     "AI/ML",
            "percent":   "50%",
            "comma":     "a,b",
            "plus":      "C++",
            "hash":      "#hashtag",
            "amp":       "R&D",
            "semicolon": "a;b",
            "bang":      "wow!",
            "question":  "why?",
            "apostroph": "what's",
            "equals":    "a=b",
            "pipe":      "a|b",
            "brace":     "{x}",
            "bracket":   "[y]",
            "tilde":     "~home",
            "dollar":    "$env",
            "at_start":  "@mention",
        }
        for label, query in cases.items():
            with self.subTest(punctuation=label, query=query):
                try:
                    md.search_memory(self.base(query=query))
                except sqlite3.OperationalError as exc:
                    self.fail(f"{label} ({query!r}) crashed search: {exc}")

    def test_dotted_term_finds_the_fact(self):
        t = self.topic("files")
        self.fact("Never centralise ltm.instructions.md into a git repo", t)
        rows = md.search_memory(self.base(query="ltm.instructions.md"))
        self.assertGreaterEqual(len(rows), 1, "dotted filename should be findable")
        self.assertTrue(any("ltm.instructions.md" in r["text"] for r in rows))

    def test_sanitizer_leaves_a_valid_quoted_phrase_intact(self):
        # Splitting on whitespace chopped '"exact phrase"' into '"exact' and
        # 'phrase"' and re-quoted both into nonsense.
        self.assertEqual(md._fts_sanitize('"exact phrase" a.b'), '"exact phrase" "a.b"')

    def test_sanitizer_preserves_prefix_search(self):
        self.assertEqual(md._fts_sanitize("junct* a.b"), 'junct* "a.b"')

    def test_hyphenated_topic_slug_is_searchable(self):
        # Slugs are the handle used everywhere; they are hyphenated by design.
        self.topic("fy27-comp-plan", title="FY27 Comp Plan")
        rows = md.search_memory(self.base(query="fy27-comp-plan"))
        self.assertGreaterEqual(len(rows), 1, "hyphenated slug not retrievable")


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


class TestTopicReferenceContract(DriverTestCase):
    """Contract test — the meta-fix for defects #16 and #19.

    Both were the same bug in different ops: a topic-referencing op accepted
    only a numeric `topic_id`, and passing the slug (the handle used everywhere
    else) failed **silently** — add_fact quietly created a global fact,
    add_snapshot raised KeyError inside a batch and returned a blank field.

    Per-op tests could not catch the *class*, because each test was written
    against the op's current signature. This enumerates every op that
    references a topic and asserts the shared contract, so a new op that
    forgets slug resolution fails here rather than in production.
    """

    #  op name -> payload builder, given the test case and a slug
    TOPIC_OPS = {
        "add_fact":     lambda t, slug: {"content": f"contract probe {slug}", "topic_slug": slug},
        "add_snapshot": lambda t, slug: {"title": "contract probe", "topic_slug": slug},
        "touch_topic":  lambda t, slug: {"topic_slug": slug},
        "sync_agent_memory": lambda t, slug: {"topic_slug": slug, "session_root": t.sessions},
    }

    # Ops that deliberately do NOT error on an unknown slug. Each must document
    # what it does instead, and assert it in test_documented_degradation.
    #   add_fact          -> degrades to a global fact
    #   sync_agent_memory -> upserts the topic; importing is pointless otherwise,
    #                        since every store_memory subject is a new topic
    TOLERATES_UNKNOWN_SLUG = {"add_fact", "sync_agent_memory"}

    def setUp(self):
        super().setUp()
        self.slug = "contract-topic"
        self.topic_id = self.topic(self.slug)
        # Fixture so sync_agent_memory has something to import; without it the op
        # would no-op and pass the contract vacuously.
        self.sessions = os.path.join(self.test_dir, "session-state")
        os.makedirs(os.path.join(self.sessions, "s1"), exist_ok=True)
        with open(os.path.join(self.sessions, "s1", "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "tool.execution_start",
                "data": {"toolName": "store_memory",
                         "arguments": {"fact": "contract probe fact",
                                       "subject": "ignored when slug forced",
                                       "scope": "user", "citations": "c"}},
            }) + "\n")

    def test_every_topic_op_accepts_topic_slug(self):
        for op, build in self.TOPIC_OPS.items():
            with self.subTest(op=op):
                res = md.DISPATCH[op](self.base(**build(self, self.slug)))
                self.assertNotIn("error", res, f"{op} rejected topic_slug: {res}")

    def test_every_topic_op_reports_an_unknown_slug(self):
        # The failure mode that hurt was silence. An unknown slug must be loud,
        # unless the op documents a deliberate degradation below.
        for op, build in self.TOPIC_OPS.items():
            if op in self.TOLERATES_UNKNOWN_SLUG:
                continue
            with self.subTest(op=op):
                res = md.DISPATCH[op](self.base(**build(self, "no-such-topic")))
                self.assertIn("error", res, f"{op} silently accepted an unknown slug")

    def test_documented_degradation(self):
        """Ops exempt from the loud-failure rule must behave as documented."""
        md.add_fact(self.base(content="degrades to global", topic_slug="no-such-topic"))
        self.assertEqual(
            self.raw("SELECT topic_id FROM facts WHERE content='degrades to global'")[0][0],
            None, "add_fact should degrade to a global fact")

        md.sync_agent_memory(self.base(session_root=self.sessions, topic_slug="brand-new-topic"))
        self.assertTrue(
            self.raw("SELECT 1 FROM topics WHERE slug='brand-new-topic'"),
            "sync_agent_memory should upsert an unknown slug")

    def test_slug_actually_attaches_the_row(self):
        # Not just "no error" — the row must land on the right topic.
        md.add_fact(self.base(content="attached by contract", topic_slug=self.slug))
        md.add_snapshot(self.base(title="attached by contract", topic_slug=self.slug))
        md.sync_agent_memory(self.base(session_root=self.sessions, topic_slug=self.slug))

        fact_topic = self.raw(
            "SELECT t.slug FROM facts f JOIN topics t ON t.id=f.topic_id "
            "WHERE f.content='attached by contract'")
        snap_topic = self.raw(
            "SELECT t.slug FROM snapshots s JOIN topics t ON t.id=s.topic_id "
            "WHERE s.title='attached by contract'")
        sync_topic = self.raw(
            "SELECT t.slug FROM facts f JOIN topics t ON t.id=f.topic_id "
            "WHERE f.content='contract probe fact'")
        self.assertEqual(fact_topic[0][0], self.slug)
        self.assertEqual(snap_topic[0][0], self.slug)
        self.assertEqual(sync_topic[0][0], self.slug,
                         "sync_agent_memory ignored the forced topic_slug")

    def test_touch_topic_still_accepts_legacy_slug_key(self):
        res = md.touch_topic(self.base(slug=self.slug))
        self.assertNotIn("error", res)

    def test_new_topic_ops_must_be_registered_here(self):
        # Tripwire: if an op gains topic_slug handling but is not listed in
        # TOPIC_OPS, this contract silently stops covering it.
        import inspect
        handled = {
            name for name, fn in md.DISPATCH.items()
            if "topic_slug" in (inspect.getsource(fn) if _has_source(fn) else "")
        }
        # export_context reads topics but takes no topic reference from callers.
        handled.discard("export_context")
        self.assertEqual(
            handled, set(self.TOPIC_OPS),
            "an op handles topic_slug but is not covered by TOPIC_OPS — add it")


def _has_source(fn):
    import inspect
    try:
        inspect.getsource(fn)
        return True
    except OSError:
        return False


class TestSyncAgentMemory(DriverTestCase):
    """sync_agent_memory replays harness store_memory calls into the local store.

    Regression guard for the two-store disconnect: facts saved through the
    agent-facing `store_memory` tool live only in the harness's own memory
    service, so export_context never sees them and they vanish at session start.
    """

    def setUp(self):
        super().setUp()
        self.sessions = os.path.join(self.test_dir, "session-state")
        os.makedirs(self.sessions)

    def write_session(self, session_id, events):
        d = os.path.join(self.sessions, session_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "events.jsonl"), "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    @staticmethod
    def call(tool, args):
        return {"type": "tool.execution_start",
                "data": {"toolName": tool, "arguments": args}}

    @staticmethod
    def store(fact, subject="testing", scope="user", citations="cite"):
        return TestSyncAgentMemory.call("store_memory", {
            "fact": fact, "subject": subject, "scope": scope,
            "citations": citations, "reason": "because"})

    def sync(self, **kw):
        return md.sync_agent_memory(self.base(session_root=self.sessions, **kw))

    def test_imports_facts_and_derives_topic_from_subject(self):
        self.write_session("s1", [self.store("Junctions beat copies", subject="skill installation")])
        r = self.sync()
        self.assertEqual(r["inserted"], 1)
        rows = self.raw("SELECT f.content, t.slug FROM facts f JOIN topics t ON t.id=f.topic_id")
        self.assertEqual(rows[0][0], "Junctions beat copies")
        self.assertEqual(rows[0][1], "skill-installation")

    def test_is_idempotent(self):
        self.write_session("s1", [self.store("Only once")])
        self.assertEqual(self.sync()["inserted"], 1)
        second = self.sync()
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["already_present"], 1)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM facts")[0][0], 1)

    def test_downvoted_fact_is_retired_not_resurrected(self):
        """Without this the sync silently republishes facts the agent retracted."""
        stale = "The repo is PUBLIC"
        self.write_session("s1", [
            self.store(stale, subject="repo privacy"),
            self.call("vote_memory", {"fact": stale, "direction": "downvote",
                                      "scope": "user", "reason": "now private"}),
        ])
        r = self.sync()
        self.assertEqual(r["superseded"], 1)
        self.assertEqual(self.raw("SELECT status FROM facts")[0][0], "superseded")

    def test_upvote_does_not_supersede(self):
        keep = "Still true"
        self.write_session("s1", [
            self.store(keep),
            self.call("vote_memory", {"fact": keep, "direction": "upvote", "scope": "user"}),
        ])
        self.sync()
        self.assertEqual(self.raw("SELECT status FROM facts")[0][0], "active")

    def test_dry_run_writes_nothing(self):
        self.write_session("s1", [self.store("Not persisted")])
        r = self.sync(dry_run=True)
        self.assertEqual(r["calls_found"], 1)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM facts")[0][0], 0)

    def test_sets_last_accessed_at(self):
        """Invariant 1: a NULL here makes the fact invisible to export_context."""
        self.write_session("s1", [self.store("Must be visible")])
        self.sync()
        self.assertEqual(
            self.raw("SELECT COUNT(*) FROM facts WHERE last_accessed_at IS NULL")[0][0], 0)

    def test_imported_fact_reaches_the_bridge(self):
        """End-to-end: the whole point is that these show up at session start."""
        self.write_session("s1", [self.store("Bridge visible fact", subject="skill installation")])
        self.sync()
        md.export_context(self.base())
        with open(self.ctx_path, encoding="utf-8") as fh:
            self.assertIn("Bridge visible fact", fh.read())

    def test_records_provenance_and_session(self):
        self.write_session("sess-abc", [self.store("Traceable", citations="from a test")])
        self.sync()
        row = self.raw("SELECT source, session_id FROM facts")[0]
        self.assertIn("store_memory", row[0])
        self.assertIn("from a test", row[0])
        self.assertEqual(row[1], "sess-abc")

    def test_ignores_other_tools_and_malformed_lines(self):
        d = os.path.join(self.sessions, "s1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("{ not json at all\n")
            fh.write(json.dumps(self.call("some_other_tool", {"fact": "nope"})) + "\n")
            fh.write(json.dumps({"type": "tool.execution_complete",
                                 "data": {"toolName": "store_memory",
                                          "arguments": {"fact": "completion event"}}}) + "\n")
            fh.write(json.dumps(self.store("the real one")) + "\n")
        r = self.sync()
        self.assertEqual(r["inserted"], 1)
        self.assertEqual(self.raw("SELECT content FROM facts")[0][0], "the real one")

    def test_missing_session_root_is_not_an_error(self):
        r = md.sync_agent_memory(self.base(session_root=os.path.join(self.test_dir, "nope")))
        self.assertEqual(r["calls_found"], 0)
        self.assertEqual(r["inserted"], 0)

    def test_since_hours_skips_old_sessions(self):
        self.write_session("old", [self.store("Ancient history")])
        path = os.path.join(self.sessions, "old", "events.jsonl")
        old = time.time() - (72 * 3600)
        os.utime(path, (old, old))
        self.write_session("new", [self.store("Recent")])
        r = self.sync(since_hours=24)
        self.assertEqual(r["inserted"], 1)
        self.assertEqual(self.raw("SELECT content FROM facts")[0][0], "Recent")

    def test_recency_uses_event_time_not_sync_time(self):
        """Stamping now() makes every imported topic look freshly used, which
        evicts genuinely active topics from the bridge (export orders by
        last_accessed_at). Recency must be when the memory was stored."""
        old_event = {"type": "tool.execution_start",
                     "timestamp": "2026-01-15T08:30:00Z",
                     "data": {"toolName": "store_memory",
                              "arguments": {"fact": "Stored back in January",
                                            "subject": "old news", "scope": "user"}}}
        self.write_session("s1", [old_event])
        self.sync()
        fact_ts = self.raw("SELECT last_accessed_at FROM facts")[0][0]
        topic_ts = self.raw("SELECT last_accessed_at FROM topics WHERE slug='old-news'")[0][0]
        self.assertTrue(fact_ts.startswith("2026-01-15"), f"fact recency was {fact_ts}")
        self.assertTrue(topic_ts.startswith("2026-01-15"), f"topic recency was {topic_ts}")

    def test_resync_does_not_bump_recency_forward(self):
        """Replaying a historical event is not a fresh relevance signal."""
        self.write_session("s1", [{"type": "tool.execution_start",
                                   "timestamp": "2026-01-15T08:30:00Z",
                                   "data": {"toolName": "store_memory",
                                            "arguments": {"fact": "Old", "subject": "x"}}}])
        self.sync()
        self.sync()
        self.assertTrue(self.raw("SELECT last_accessed_at FROM facts")[0][0].startswith("2026-01-15"))

    def test_dispatch_registered(self):
        """Registration is what makes the op usable inside batch."""
        self.assertIn("sync_agent_memory", md.DISPATCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
