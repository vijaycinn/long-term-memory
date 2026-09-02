import json
import sys
import tempfile
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import retrieval_bench as bench


class RetrievalBenchTests(unittest.TestCase):
    def write_golden(self, root, entries):
        path = root / "queries.json"
        path.write_text(json.dumps({"_comment": "page stems", "queries": entries}), encoding="utf-8")
        return path

    def entry(self, identifier, page="Answer", cls="local"):
        return {
            "id": identifier, "query": f"query {identifier}", "expect_pages": [page],
            "expect_facts": [], "class": cls,
        }

    def test_golden_schema_and_expected_pages_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            target = vault / "nested" / "Answer.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Answer", encoding="utf-8")
            entries = [self.entry(f"g{i:02}", cls="paraphrase" if i < 8 else
                                  "multihop" if i < 11 else "local") for i in range(25)]
            loaded = bench.load_golden(self.write_golden(root, entries), vault)
            self.assertEqual(25, len(loaded))
            self.assertEqual(target, bench.resolve_page("Answer", vault))

    def test_golden_rejects_unresolved_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            entries = [self.entry(f"g{i:02}", page="Missing", cls="paraphrase" if i < 8 else
                                  "multihop" if i < 11 else "local") for i in range(25)]
            with self.assertRaisesRegex(bench.BenchError, "unresolved expected pages"):
                bench.load_golden(self.write_golden(root, entries), vault)

    def test_metrics_recall_and_mrr(self):
        entries = [
            self.entry("g01", "one"),
            self.entry("g02", "two"),
            self.entry("g03", "three"),
        ]
        results = [["one"], ["miss", "two"], ["a", "b", "c", "d", "e", "three"]]
        actual = bench.metrics(entries, results, [1.0, 2.0, 3.0])
        self.assertAlmostEqual(2 / 3, actual["recall_at_5"])
        self.assertAlmostEqual(.5, actual["mrr"])
        self.assertAlmostEqual(2.0, actual["p50_ms"])
        self.assertAlmostEqual(3.0, actual["p95_ms"])

    def test_compare_subtracts_after_from_before(self):
        report_a = {"paths": {"lexical_v0": {"status": "ok", "overall": {
            "recall_at_5": .2, "mrr": .1, "p50_ms": 10, "p95_ms": 20},
            "classes": {"paraphrase": {"recall_at_5": 0, "mrr": 0, "p50_ms": 11, "p95_ms": 21}}}}}
        report_b = {"paths": {"lexical_v0": {"status": "ok", "overall": {
            "recall_at_5": .5, "mrr": .4, "p50_ms": 8, "p95_ms": 25},
            "classes": {"paraphrase": {"recall_at_5": .25, "mrr": .5, "p50_ms": 9, "p95_ms": 19}}}}}
        rows = {row["class"]: row for row in bench.compare(report_a, report_b)}
        self.assertAlmostEqual(.3, rows["all"]["recall_at_5"])
        self.assertAlmostEqual(.3, rows["all"]["mrr"])
        self.assertAlmostEqual(-2, rows["all"]["p50_ms"])
        self.assertAlmostEqual(5, rows["all"]["p95_ms"])
        self.assertAlmostEqual(.25, rows["paraphrase"]["recall_at_5"])


if __name__ == "__main__":
    unittest.main()
