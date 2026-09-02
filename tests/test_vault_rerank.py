# Repository is organised by function; nothing is installed as a package, so the
# sibling directories are put on sys.path before importing anything from them.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_WIKI = _os.path.join(_REPO, "wiki")
if _WIKI not in _sys.path:
    _sys.path.insert(0, _WIKI)

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vault_rerank as rerank


class RerankTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "04-Tech").mkdir(parents=True)
        (self.vault / "01-Activity" / "Sessions").mkdir(parents=True)
        (self.vault / "04-Tech" / "GCC Azure Architecture.md").write_text(
            "---\nsummary: \"GCC uses commercial Azure.\"\n---\n", encoding="utf-8")
        (self.vault / "01-Activity" / "Sessions" / "Work Log 2026-05.md").write_text(
            "---\nsummary: \"A broad record.\"\n---\n", encoding="utf-8")
        self.rows = [
            {"path": "01-Activity/Sessions/Work Log 2026-05.md", "rrf": .032,
             "sources": ["bm25#1", "vector#2"]},
            {"path": "04-Tech/GCC Azure Architecture.md", "rrf": .030,
             "sources": ["bm25#3", "vector#4"]},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_canonical_title_and_path_beat_a_generic_work_log(self):
        with patch.object(rerank, "_page_semantics", return_value=({}, {})):
            out = rerank.rerank("GCC availability", self.rows, 2, self.vault)
        self.assertEqual(out[0]["path"], "04-Tech/GCC Azure Architecture.md")
        self.assertIn("rerank_score", out[0])

    def test_page_semantics_are_page_level_not_result_chunk_level(self):
        with patch.object(
            rerank, "_page_semantics",
            return_value=({
                "01-Activity/Sessions/Work Log 2026-05.md": .50,
                "04-Tech/GCC Azure Architecture.md": 1.0,
            }, {
                "01-Activity/Sessions/Work Log 2026-05.md": 20,
                "04-Tech/GCC Azure Architecture.md": 1,
            }),
        ):
            out = rerank.rerank("GCC availability", self.rows, 2, self.vault)
        self.assertEqual(out[0]["path"], "04-Tech/GCC Azure Architecture.md")

    def test_ties_use_a_stable_path_order(self):
        rows = [
            {"path": "04-Tech/Topic B.md", "rrf": .01, "sources": []},
            {"path": "04-Tech/Topic A.md", "rrf": .01, "sources": []},
        ]
        with patch.object(rerank, "_page_semantics", return_value=({}, {})):
            out = rerank.rerank("Topic unrelated", rows, 2, self.vault)
        self.assertEqual([row["path"] for row in out],
                         ["04-Tech/Topic A.md", "04-Tech/Topic B.md"])

    def test_failure_preserves_existing_order(self):
        with patch.object(rerank, "_page_semantics", side_effect=RuntimeError("unavailable")):
            self.assertEqual(rerank.rerank("GCC", self.rows, 2, self.vault), self.rows)


if __name__ == "__main__":
    unittest.main()
