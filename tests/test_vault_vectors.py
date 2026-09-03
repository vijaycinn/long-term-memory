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

import vault_vectors as vv


class CleanTextTests(unittest.TestCase):
    """Boilerplate must not reach the embedder.

    An audit measured 61% of the vault's wikilinks as auto-generated '## Related'
    blocks and breadcrumbs. Embedding those drags every page toward a common
    centroid and actively degrades semantic search.
    """

    def test_strips_frontmatter(self):
        out = vv.clean_text('---\nsummary: "x"\ngenerated: true\n---\n\nReal body.\n')
        self.assertNotIn("generated", out)
        self.assertIn("Real body.", out)

    def test_strips_related_block(self):
        out = vv.clean_text("Body text here.\n\n## Related\n\n- [[A]]\n- [[B]]\n")
        self.assertIn("Body text here.", out)
        self.assertNotIn("Related", out)

    def test_related_block_stops_at_next_heading(self):
        out = vv.clean_text(
            "Intro.\n\n## Related\n\n- [[A]]\n\n## Findings\n\nKeep this.\n")
        self.assertIn("Keep this.", out)
        self.assertIn("Findings", out)
        self.assertNotIn("[[A]]", out)

    def test_strips_breadcrumb(self):
        out = vv.clean_text("[[Home]] › [[MOC]] › [[Page]]\n\nActual content.\n")
        self.assertIn("Actual content.", out)
        self.assertNotIn("MOC", out)

    def test_keeps_readable_half_of_wikilink(self):
        self.assertIn("Contoso", vv.clean_text("See [[Account - Contoso|Contoso]] now."))
        self.assertNotIn("[[", vv.clean_text("See [[Account - Contoso|Contoso]] now."))

    def test_wikilink_without_alias_keeps_target(self):
        out = vv.clean_text("Refer to [[SAIF]] here.")
        self.assertIn("SAIF", out)
        self.assertNotIn("[[", out)

    def test_wikilink_with_heading_anchor(self):
        out = vv.clean_text("See [[Page#Section]].")
        self.assertNotIn("#Section", out)
        self.assertIn("Page", out)


class ChunkTests(unittest.TestCase):

    def test_short_text_is_one_chunk(self):
        self.assertEqual(vv.chunk("a short note that is definitely long enough here."),
                         ["a short note that is definitely long enough here."])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(vv.chunk(""), [])
        self.assertEqual(vv.chunk("   \n\n  "), [])

    def test_long_text_splits_and_covers_everything(self):
        para = ("Sentence about retrieval quality. " * 12).strip()
        text = "\n\n".join([para] * 8)
        pieces = vv.chunk(text, size=400, overlap=50)
        self.assertGreater(len(pieces), 1)
        # Overlap means total length exceeds the original; nothing may be lost.
        self.assertGreaterEqual(sum(len(p) for p in pieces), len(text) * 0.9)

    def test_chunks_respect_size_budget(self):
        text = "word " * 2000
        for piece in vv.chunk(text, size=500, overlap=50):
            self.assertLessEqual(len(piece), 700)   # size + a boundary allowance

    def test_tiny_trailing_fragment_is_dropped(self):
        # A heading fragment is noise, not a retrievable idea.
        pieces = vv.chunk("x" * 1000 + "\n\nab", size=1000, overlap=10)
        self.assertTrue(all(len(p) >= vv.MIN_CHUNK_CHARS for p in pieces))

    def test_termination_on_pathological_input(self):
        # No paragraph, sentence or space boundaries anywhere: must still halt.
        pieces = vv.chunk("z" * 5000, size=500, overlap=100)
        self.assertGreater(len(pieces), 1)


class HashTests(unittest.TestCase):
    """Content hashing is what makes a committed sidecar durable."""

    def test_same_text_same_hash(self):
        self.assertEqual(vv._hash("hello world"), vv._hash("hello world"))

    def test_different_text_different_hash(self):
        self.assertNotEqual(vv._hash("hello world"), vv._hash("hello worlds"))

    def test_hash_is_stable_across_processes(self):
        # sha256-derived, not Python's salted hash(); a committed DB built in
        # one process must still validate in the next.
        self.assertEqual(vv._hash("stability"), vv._hash("stability"))
        self.assertEqual(len(vv._hash("x")), 32)


class IndexTests(unittest.TestCase):
    """Indexing tests use a tiny sqlite-vec-compatible fixture, never live state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        (self.vault / "alpha.md").write_text(
            "---\nsummary: \"Canonical alpha summary\"\n---\n"
            "# Alpha page\n\n"
            "[[Home]] › [[Research]] › [[Alpha page]]\n\n"
            "## First section\n\n"
            "Alpha content explains the indexed idea.\n",
            encoding="utf-8",
        )
        (self.vault / "beta.md").write_text(
            "---\nsummary: \"Canonical beta summary\"\n---\n"
            "# Beta page\n\nBeta content stays out of a one-page run.\n",
            encoding="utf-8",
        )
        self.primary = Path(self.temp.name) / "primary.db"
        self.shadow = Path(self.temp.name) / "shadow.db"
        self._originals = {
            "_sqlite_vec": vv._sqlite_vec,
            "_connect": vv._connect,
            "_init": vv._init,
            "dimensions": vv.vault_embed.dimensions,
            "embed": vv.vault_embed.embed,
            "config_get": vv.config.get,
        }

        class FakeVec:
            @staticmethod
            def load(con):
                return None

        def connect(path, create=False):
            path = Path(path)
            if not create and not path.is_file():
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            return con

        def init(con, dims):
            con.execute(
                "CREATE TABLE IF NOT EXISTS vec_chunks "
                "(rowid INTEGER PRIMARY KEY, embedding BLOB)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "rowid INTEGER PRIMARY KEY, path TEXT NOT NULL, "
                "chunk_ix INTEGER NOT NULL, content_hash TEXT NOT NULL, "
                "preview TEXT, UNIQUE(path, chunk_ix))"
            )
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                "body, path UNINDEXED, chunk_ix UNINDEXED)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS query_cache ("
                "key TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
            )

        def get(key, override=None):
            if key == "embed_endpoint":
                return "https://example.invalid/embed"
            return self._originals["config_get"](key, override)

        vv._sqlite_vec = lambda: FakeVec
        vv._connect = connect
        vv._init = init
        vv.vault_embed.dimensions = lambda: 3
        vv.vault_embed.embed = lambda texts: [
            [float(index + 1), float(index + 2), float(index + 3)]
            for index, _ in enumerate(texts)
        ]
        vv.config.get = get

    def tearDown(self):
        vv._sqlite_vec = self._originals["_sqlite_vec"]
        vv._connect = self._originals["_connect"]
        vv._init = self._originals["_init"]
        vv.vault_embed.dimensions = self._originals["dimensions"]
        vv.vault_embed.embed = self._originals["embed"]
        vv.config.get = self._originals["config_get"]
        self.temp.cleanup()

    def _rows(self, db, sql, params=()):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_context_header_is_indexed_but_preview_stays_clean(self):
        stats = vv.index(self.vault, self.shadow, verbose=False, contextual=True)
        self.assertTrue(stats["ok"])
        indexed = self._rows(
            self.shadow, "SELECT body FROM chunk_fts WHERE path = 'alpha.md'")
        preview = self._rows(
            self.shadow, "SELECT preview FROM chunks WHERE path = 'alpha.md'")
        self.assertTrue(indexed)
        self.assertIn("Page: Alpha page", indexed[0]["body"])
        self.assertIn("Area: Home > Research", indexed[0]["body"])
        self.assertIn("Summary: Canonical alpha summary", indexed[0]["body"])
        self.assertNotIn("Page: Alpha page", preview[0]["preview"])
        self.assertNotIn("Summary: Canonical alpha summary", preview[0]["preview"])

    def test_context_header_is_bounded(self):
        raw = "---\nsummary: \"" + ("summary " * 200) + "\"\n---\n# " + ("title " * 50)
        header = vv._context_header("folder/page.md", raw, "# Section\n\nbody", 0)
        self.assertLessEqual(len(header), vv.CONTEXT_HEADER_MAX_CHARS)

    def test_context_header_uses_nearest_section(self):
        body = "# Page\n\n## Retrieval section\n\nThe answer is here."
        header = vv._context_header("folder/page.md", body, body, body.index("The"))
        self.assertIn("Section: Retrieval section", header)

    def test_limited_index_preserves_out_of_scope_chunks(self):
        first, second = [p.relative_to(self.vault).as_posix()
                         for p in vv._pages(self.vault)]
        vv.index(self.vault, self.primary, verbose=False)
        before = vv.status(self.primary)
        stats = vv.index(self.vault, self.primary, verbose=False, limit_pages=1)
        after = vv.status(self.primary)
        self.assertTrue(stats["ok"])
        self.assertEqual(before["pages"], 2)
        self.assertEqual(after["pages"], 2)
        self.assertEqual(
            {row["path"] for row in self._rows(
                self.primary, "SELECT path FROM chunks")},
            {first, second},
        )
        self.assertEqual(
            {row["path"] for row in self._rows(
                self.primary, "SELECT path FROM chunk_fts")},
            {first, second},
        )

    def test_alternate_db_does_not_modify_primary(self):
        vv.index(self.vault, self.primary, verbose=False)
        before = vv.status(self.primary)
        vv.index(self.vault, self.shadow, verbose=False, contextual=True)
        after = vv.status(self.primary)
        shadow = vv.status(self.shadow)
        self.assertEqual(after["chunks"], before["chunks"])
        self.assertEqual(after["pages"], before["pages"])
        self.assertEqual(
            self._rows(self.primary, "SELECT body FROM chunk_fts "
                                      "WHERE body LIKE 'Page:%'"),
            [],
        )
        self.assertTrue(self._rows(self.shadow, "SELECT body FROM chunk_fts "
                                               "WHERE body LIKE 'Page:%'"))
        self.assertEqual(shadow["chunks"], before["chunks"])
        self.assertEqual(shadow["pages"], before["pages"])

    def test_partial_embedding_is_not_reported_as_success(self):
        for i in range(65):
            (self.vault / f"page-{i:02d}.md").write_text(
                f"# Page {i}\n\nUnique body {i} long enough to become an indexed chunk. "
                + ("detail " * 20),
                encoding="utf-8",
            )
        calls = 0

        def fail_second_batch(texts):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [[1.0, 0.0, 0.0] for _ in texts]
            return []

        vv.vault_embed.embed = fail_second_batch
        stats = vv.index(self.vault, self.shadow, verbose=False, contextual=True)

        self.assertFalse(stats["ok"])
        self.assertTrue(stats["partial"])
        self.assertEqual(stats["embedded"], 64)
        self.assertGreater(stats["pending"], stats["embedded"])


class PackTests(unittest.TestCase):

    def test_roundtrip(self):
        import struct
        vec = [0.5, -0.25, 1.0]
        blob = vv._pack(vec)
        back = list(struct.unpack(f"{len(blob)//4}f", blob))
        for a, b in zip(vec, back):
            self.assertAlmostEqual(a, b, places=6)


class SanitiseFtsTests(unittest.TestCase):
    """Paths, emails and filenames are exactly what gets searched, and every one
    contains punctuation FTS5 parses as an operator."""

    def test_plain_words_untouched(self):
        self.assertEqual(vv._sanitise_fts("azure cost"), "azure cost")

    def test_operators_preserved(self):
        self.assertEqual(vv._sanitise_fts("azure OR aws"), "azure OR aws")

    def test_prefix_wildcard_preserved(self):
        self.assertEqual(vv._sanitise_fts("azur*"), "azur*")

    def test_punctuation_is_quoted(self):
        self.assertIn('"', vv._sanitise_fts("bob@example.com"))
        self.assertIn('"', vv._sanitise_fts(r"C:\path\file.md"))

    def test_embedded_quote_is_escaped(self):
        self.assertNotIn('""""', vv._sanitise_fts('say"hi'))


class Bm25RecallTests(unittest.TestCase):
    """The lexical side of hybrid retrieval must not disappear for questions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "vectors.db"
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.execute("""CREATE VIRTUAL TABLE chunk_fts USING fts5(
            body, path UNINDEXED, chunk_ix UNINDEXED)""")
        con.executemany(
            "INSERT INTO chunk_fts (body, path, chunk_ix) VALUES (?,?,?)",
            [
                ("Content Understanding pricing makes scanned page extraction cost predictable.",
                 "pricing.md", 0),
                ("How much does the service cost before deployment?",
                 "faq.md", 0),
                ("GCC GCC GCC commercial Azure architecture uses a separate Entra tenant.",
                 "gcc-current.md", 0),
                ("GCC legacy environment is retired.", "gcc-legacy.md", 0),
                ("The exact phrase appears in this note.", "phrase.md", 0),
                ("Architecture guidance for current deployments.", "architecture.md", 0),
                ("Contact alex@example.com for the deployment review.", "contact.md", 0),
                ("How to deploy a service safely.", "how-to.md", 0),
            ],
        )
        con.commit()
        con.close()
        self._connect = vv._connect

        def connect_fixture(path, create=False):
            fixture = sqlite3.connect(path)
            fixture.row_factory = sqlite3.Row
            return fixture

        vv._connect = connect_fixture

    def tearDown(self):
        vv._connect = self._connect
        self.temp.cleanup()

    def test_natural_language_query_relaxes_to_content_terms(self):
        hits = vv.search_bm25(
            "how much does content understanding cost per page", limit=3, db=self.db)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "pricing.md")
        self.assertEqual(hits[0]["branch"], "relaxed")

    def test_short_precise_query_keeps_existing_top_hit(self):
        self.assertEqual(vv.search_bm25("GCC", limit=3, db=self.db)[0]["path"],
                         "gcc-current.md")

    def test_deliberate_phrase_syntax_stays_exact(self):
        self.assertEqual(vv.search_bm25('"exact phrase"', limit=3, db=self.db)[0]["path"],
                         "phrase.md")

    def test_deliberate_prefix_syntax_stays_exact(self):
        self.assertEqual(vv.search_bm25("arch*", limit=3, db=self.db)[0]["path"],
                         "architecture.md")

    def test_deliberate_not_syntax_stays_exact(self):
        paths = [hit["path"] for hit in vv.search_bm25("GCC NOT legacy", 3, self.db)]
        self.assertEqual(paths, ["gcc-current.md"])

    def test_punctuation_token_is_sanitised_and_matched(self):
        self.assertEqual(vv.search_bm25("alex@example.com", 3, self.db)[0]["path"],
                         "contact.md")

    def test_all_stopword_query_is_not_relaxed_to_broad_or(self):
        self.assertEqual(vv._relaxed_fts("how to"), "how to")
        self.assertEqual(vv.search_bm25("how to", 3, self.db)[0]["path"], "how-to.md")

    def test_relaxed_bm25_cannot_displace_vector_hit(self):
        query = "how much does content understanding cost per page"
        original_search = vv.search
        vv.search = lambda q, l=10, db=None: [{
            "path": "vector-winner.md", "rank": 1, "distance": 0.01,
        }]
        try:
            out = vv.search_hybrid(query, limit=1, db=self.db)
        finally:
            vv.search = original_search
        self.assertEqual(out[0]["path"], "vector-winner.md")


class HybridFusionTests(unittest.TestCase):
    """RRF fuses ranks, not scores -- bm25 values and cosine distances are not on
    comparable scales, so normalising them against each other would be arbitrary."""

    def setUp(self):
        self._bm25, self._vec = vv.search_bm25, vv.search

    def tearDown(self):
        vv.search_bm25, vv.search = self._bm25, self._vec

    def _stub(self, bm25_paths, vector_paths):
        vv.search_bm25 = lambda q, l=10, db=None: [
            {"path": p, "rank": i, "score": -float(i)}
            for i, p in enumerate(bm25_paths, 1)]
        vv.search = lambda q, l=10, db=None: [
            {"path": p, "rank": i, "distance": i / 10.0}
            for i, p in enumerate(vector_paths, 1)]

    def test_agreed_document_outranks_either_alone(self):
        # 'both' is 2nd in each list; 'lex_only' is 1st in one and absent from
        # the other. Agreement across retrievers should win.
        self._stub(["lex_only", "both"], ["vec_only", "both"])
        out = vv.search_hybrid("q", limit=3)
        self.assertEqual(out[0]["path"], "both")

    def test_scores_match_the_rrf_formula(self):
        self._stub(["a"], ["a"])
        out = vv.search_hybrid("q", limit=1)
        self.assertAlmostEqual(out[0]["rrf"], 2.0 / (vv.RRF_K + 1), places=9)

    def test_records_which_retrievers_contributed(self):
        self._stub(["a"], ["a"])
        self.assertEqual(sorted(vv.search_hybrid("q", 1)[0]["sources"]),
                         ["bm25#1", "vector#1"])

    def test_degrades_to_the_surviving_retriever(self):
        # No sqlite-vec, no endpoint, or a lapsed az login: vector returns [].
        self._stub(["a", "b"], [])
        out = vv.search_hybrid("q", limit=2)
        self.assertEqual([d["path"] for d in out], ["a", "b"])

    def test_returns_empty_when_both_retrievers_fail(self):
        self._stub([], [])
        self.assertEqual(vv.search_hybrid("q", limit=5), [])

    def test_blank_query_returns_empty(self):
        self.assertEqual(vv.search_hybrid("   ", limit=5), [])


if __name__ == "__main__":
    unittest.main()
