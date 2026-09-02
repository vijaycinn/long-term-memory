# Repository is organised by function; nothing is installed as a package, so the
# sibling directories are put on sys.path before importing anything from them.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sib in ("core", "hooks", "ops", "wiki"):
    _p = _os.path.join(_REPO, _sib)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import sqlite3
import tempfile
import unittest
from pathlib import Path

import vault_mcp

SCHEMA = Path(_REPO) / "setup" / "init-memory.sql"


def _fresh_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.row_factory = sqlite3.Row
    return con


def _add_topic(con, slug, title, category="research"):
    cur = con.execute(
        "INSERT INTO topics (slug, title, category) VALUES (?,?,?)", (slug, title, category))
    return cur.lastrowid


def _add_fact(con, content, topic_id=None, importance=3, fact_type="finding"):
    cur = con.execute(
        "INSERT INTO facts (content, fact_type, topic_id, importance) VALUES (?,?,?,?)",
        (content, fact_type, topic_id, importance))
    return cur.lastrowid


def _add_entity(con, name, entity_type="account"):
    cur = con.execute(
        "INSERT INTO entities (name, entity_type) VALUES (?,?)", (name, entity_type))
    return cur.lastrowid


def _link(con, entity_id, fact_id):
    con.execute("INSERT OR IGNORE INTO entity_mentions "
                "(entity_id, parent_id, parent_type, relationship) VALUES (?,?,?,'mentions')",
                (entity_id, fact_id, "fact"))


class BlendDirectionTests(unittest.TestCase):
    """The bm25 sign trap, pinned.

    SQLite's bm25() is negative and more-negative-is-better. If a future change
    sorts it descending, or normalises the wrong way round, relevance inverts
    silently -- the tool keeps returning plausible facts, so nothing fails
    loudly. These tests fail loudly.
    """

    def test_most_negative_bm25_normalises_to_one(self):
        rows = [
            {"bm25_rank": -9.0, "importance": 1, "content": "best"},
            {"bm25_rank": -1.0, "importance": 1, "content": "worst"},
        ]
        out = vault_mcp._blend(rows, limit=2)
        self.assertEqual(out[0]["content"], "best")
        self.assertAlmostEqual(out[0]["rank_norm"], 1.0)
        self.assertAlmostEqual(out[1]["rank_norm"], 0.0)

    def test_importance_cannot_override_a_large_relevance_gap(self):
        # importance contributes at most IMPORTANCE_WEIGHT (0.3); a full-span
        # relevance win is worth RANK_WEIGHT (0.7). Relevance must win.
        rows = [
            {"bm25_rank": -9.0, "importance": 1, "content": "relevant but unimportant"},
            {"bm25_rank": -1.0, "importance": 5, "content": "important but irrelevant"},
        ]
        out = vault_mcp._blend(rows, limit=2)
        self.assertEqual(out[0]["content"], "relevant but unimportant")

    def test_importance_breaks_ties_when_relevance_is_equal(self):
        rows = [
            {"bm25_rank": -5.0, "importance": 2, "content": "low"},
            {"bm25_rank": -5.0, "importance": 5, "content": "high"},
        ]
        out = vault_mcp._blend(rows, limit=2)
        self.assertEqual(out[0]["content"], "high")

    def test_degenerate_cases(self):
        self.assertEqual(vault_mcp._blend([], limit=5), [])
        single = vault_mcp._blend([{"bm25_rank": -3.0, "importance": 3, "content": "x"}], limit=5)
        self.assertEqual(len(single), 1)
        self.assertAlmostEqual(single[0]["rank_norm"], 1.0)


class RareTermTests(unittest.TestCase):
    """End-to-end against a real FTS5 index: a rare precise term must outrank a
    generic importance-5 fact. This is the acceptance test for T1.2."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "m.db"
        self.con = _fresh_db(self.db)

    def tearDown(self):
        self.con.close()
        self._tmp.cleanup()

    def test_rare_term_outranks_generic_importance_five(self):
        t = _add_topic(self.con, "t", "Topic")
        # Importance 5, but only an incidental mention of the term.
        _add_fact(self.con, "Azure is the platform we use for everything.",
                  topic_id=t, importance=5)
        for i in range(8):
            _add_fact(self.con, f"Azure general note number {i} about the platform.",
                      topic_id=t, importance=5)
        # Importance 1, but densely on-topic for the rare term.
        target = _add_fact(
            self.con,
            "Zephyrcascade quota tuning: the Zephyrcascade limiter caps Zephyrcascade throughput.",
            topic_id=t, importance=1)
        self.con.commit()

        rows = vault_mcp._fts_query(self.con, "Zephyrcascade", 5)
        self.assertTrue(rows, "rare term returned nothing")
        self.assertEqual(rows[0]["fact_id"], target,
                         "importance-1 rare-term fact did not outrank importance-5 noise")

    def test_sql_pool_is_filled_from_the_most_relevant_end(self):
        """Pins the SQL ORDER BY direction, which _blend alone cannot protect.

        _blend re-sorts by score, so on a small fixture the candidate pool holds
        every row and the SQL direction is invisible -- a mutation flipping it to
        `ORDER BY bm25_rank DESC` passed every other test in this file. Against
        the real 818-fact database that mutation pools the 40 WORST matches and
        inverts relevance silently. So the fixture here is deliberately larger
        than the pool (max(limit*FTS_OVERFETCH, MIN_FTS_POOL)); the target is
        only reachable if the pool is filled from the best end.
        """
        t = _add_topic(self.con, "t", "Topic")
        pool = max(5 * vault_mcp.FTS_OVERFETCH, vault_mcp.MIN_FTS_POOL)
        filler = " ".join(f"padding{i}" for i in range(60))
        for i in range(pool + 20):
            _add_fact(self.con, f"Zephyrcascade mentioned once. {filler} note {i}.",
                      topic_id=t, importance=5)
        # Short and dense: highest term frequency, shortest document -> best bm25.
        target = _add_fact(
            self.con,
            "Zephyrcascade Zephyrcascade Zephyrcascade Zephyrcascade Zephyrcascade.",
            topic_id=t, importance=1)
        self.con.commit()

        rows = vault_mcp._fts_query(self.con, "Zephyrcascade", 5)
        self.assertTrue(rows, "rare term returned nothing")
        self.assertEqual(
            rows[0]["fact_id"], target,
            "candidate pool was not filled from the most-relevant end -- "
            "check the ORDER BY direction in _fts_query")

    def test_punctuation_query_does_not_raise(self):
        t = _add_topic(self.con, "t", "Topic")
        _add_fact(self.con, "See C:\\path\\to\\file.md and mail bob@example.com", topic_id=t)
        self.con.commit()
        # Must fall through to _sanitise_fts rather than raising OperationalError.
        vault_mcp._fts_query(self.con, "C:\\path\\to\\file.md", 5)
        vault_mcp._fts_query(self.con, "bob@example.com", 5)


class RelatedFactsTests(unittest.TestCase):
    """T1.3: entity co-mention expansion, with its caps and exclusions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "m.db"
        self.con = _fresh_db(self.db)

    def tearDown(self):
        self.con.close()
        self._tmp.cleanup()

    def test_returns_co_mentioned_facts_from_other_topics(self):
        t1 = _add_topic(self.con, "a", "Alpha")
        t2 = _add_topic(self.con, "b", "Beta")
        ent = _add_entity(self.con, "Contoso")
        hit = _add_fact(self.con, "Contoso signed the agreement.", topic_id=t1)
        other = _add_fact(self.con, "Contoso runs a legacy estate.", topic_id=t2)
        _link(self.con, ent, hit)
        _link(self.con, ent, other)
        self.con.commit()

        rel = vault_mcp._related_facts(self.con, [hit])
        ids = [r["id"] if "id" in r.keys() else r["fact_id"] for r in rel]
        self.assertIn(other, ids)

    def test_direct_hits_are_never_returned_as_related(self):
        t = _add_topic(self.con, "a", "Alpha")
        ent = _add_entity(self.con, "Contoso")
        hit = _add_fact(self.con, "Contoso signed the agreement.", topic_id=t)
        _link(self.con, ent, hit)
        self.con.commit()

        rel = vault_mcp._related_facts(self.con, [hit])
        ids = [r["id"] if "id" in r.keys() else r["fact_id"] for r in rel]
        self.assertNotIn(hit, ids)

    def test_cap_is_honoured(self):
        t = _add_topic(self.con, "a", "Alpha")
        ent = _add_entity(self.con, "Contoso")
        hit = _add_fact(self.con, "Contoso signed.", topic_id=t)
        _link(self.con, ent, hit)
        for i in range(20):
            fid = _add_fact(self.con, f"Contoso note {i}.", topic_id=t)
            _link(self.con, ent, fid)
        self.con.commit()

        rel = vault_mcp._related_facts(self.con, [hit])
        self.assertLessEqual(len(rel), vault_mcp.RELATED_FACT_CAP)

    def test_no_edges_yields_nothing_rather_than_erroring(self):
        t = _add_topic(self.con, "a", "Alpha")
        hit = _add_fact(self.con, "A fact with no entity edges.", topic_id=t)
        self.con.commit()
        self.assertEqual(vault_mcp._related_facts(self.con, [hit]), [])


class HopsByteIdentityTests(unittest.TestCase):
    """T1.1 rollback guarantee: hops unset must behave exactly as before."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        (self.vault / "sub").mkdir(parents=True)
        (self.vault / "Target.md").write_text(
            '---\nsummary: "The neighbour page."\n---\n\n# Target\n\nBody.\n',
            encoding="utf-8")
        (self.vault / "Source.md").write_text(
            '---\nsummary: "The source page."\n---\n\n# Source\n\n'
            'Inline reference to [[Target]] in the body.\n\n'
            '## Related\n\n- [[Target]]\n',
            encoding="utf-8")
        self._orig = vault_mcp.VAULT
        vault_mcp.VAULT = self.vault
        vault_mcp._CACHE.clear()
        vault_mcp._stem_index(force=True)

    def tearDown(self):
        vault_mcp.VAULT = self._orig
        vault_mcp._CACHE.clear()
        vault_mcp._stem_index(force=True)
        self._tmp.cleanup()

    def test_hops_zero_omits_linked_block(self):
        out = vault_mcp.t_read({"page": "Source"})
        self.assertNotIn("Linked pages", out)

    def test_hops_zero_matches_explicit_zero(self):
        self.assertEqual(vault_mcp.t_read({"page": "Source"}),
                         vault_mcp.t_read({"page": "Source", "hops": 0}))

    def test_hops_one_appends_neighbour_titles_not_bodies(self):
        out = vault_mcp.t_read({"page": "Source", "hops": 1})
        self.assertIn("Linked pages", out)
        self.assertIn("Target", out)
        self.assertIn("The neighbour page.", out)
        # The neighbour's body must NOT be inlined -- that is the context blowout.
        self.assertNotIn("Body.", out)

    def test_linked_block_respects_char_cap(self):
        out = vault_mcp.t_read({"page": "Source", "hops": 1})
        block = out.split("Linked pages", 1)[1]
        self.assertLessEqual(len(block), vault_mcp.NEIGHBOUR_CHAR_CAP + 200)


if __name__ == "__main__":
    unittest.main()
