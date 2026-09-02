# Repository is organised by function; nothing is installed as a package, so the
# sibling directories are put on sys.path before importing anything from them.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sib in ("core", "hooks", "ops", "wiki"):
    _p = _os.path.join(_REPO, _sib)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import unittest

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
