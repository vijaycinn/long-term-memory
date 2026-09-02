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
        self.assertAlmostEqual(1.0, actual["recall_at_10"])
        self.assertAlmostEqual(1.0, actual["recall_at_20"])
        self.assertAlmostEqual(.5, actual["mrr"])
        self.assertAlmostEqual(2.0, actual["p50_ms"])
        self.assertAlmostEqual(3.0, actual["p95_ms"])

    def test_oracle_metrics_reports_cutoffs_and_reranker_headroom(self):
        entries = [
            self.entry("g01", "One.2"),
            self.entry("g02", "Two"),
            self.entry("g03", "Three"),
            {
                **self.entry("g04", "Four"),
                "expect_facts": ["Four is a fact that must not score"],
            },
        ]
        pools = [
            [{"path": "nested/One.2.md"}],
            [{"path": f"miss-{i}.md"} for i in range(7)]
            + [{"path": "nested/Two.md"}],
            [{"path": f"miss-{i}.md"} for i in range(14)]
            + [{"path": "nested/Three.md"}],
            [{"path": f"miss-{i}.md"} for i in range(20)],
        ]

        actual = bench.oracle_metrics(entries, pools)

        self.assertEqual({"hits": 1, "queries": 4, "rate": .25, "percentage": 25.0},
                         actual["recall_at_5"])
        self.assertEqual({"hits": 2, "queries": 4, "rate": .5, "percentage": 50.0},
                         actual["recall_at_10"])
        self.assertEqual({"hits": 3, "queries": 4, "rate": .75, "percentage": 75.0},
                         actual["recall_at_20"])
        self.assertEqual(
            {"additional_hits": 2, "queries": 4, "rate": .5, "percentage_points": 50.0},
            actual["reranker_headroom"],
        )
        self.assertAlmostEqual(1 / 4, actual["mrr"])

    def test_current_vault_parser_accepts_ranked_and_lexical_markers(self):
        rendered = "\n".join([
            "2 match(es) for 'example' [hybrid]",
            "",
            "  [0.0328] PageName",
            "           nested/PageName.md",
            "[  12] LexicalPage",
            "       nested/LexicalPage.md",
            "           continuation summary",
        ])
        self.assertEqual(
            ["PageName", "LexicalPage"],
            [
                match.group("stem")
                for line in rendered.splitlines()
                if (match := bench._RANKED_RESULT_RE.match(line))
            ],
        )

    def test_current_memory_parser_excludes_related_facts(self):
        direct = "Direct fact content"
        rendered = "\n".join([
            "1 match(es) for 'example'",
            "",
            f"- (finding imp5) [topic] {direct}",
            "    source: source",
            "",
            "## related_facts (1) - co-mentioned via a shared entity, NOT matches for the query",
            "+ (finding imp5) [topic] Related fact content",
            "    via entity: Example",
        ])
        self.assertEqual(
            [direct],
            [
                match.group("content")
                for line in rendered.splitlines()
                if (match := bench._MEMORY_RESULT_RE.match(line))
            ],
        )

    def test_repository_golden_set_validates(self):
        loaded = bench.load_golden()
        self.assertGreaterEqual(len(loaded), 25)

    def test_repository_provenance_covers_every_query(self):
        # Sidecar file, not part of the frozen load_golden() schema (which rejects any
        # unexpected key). Two provenance_type values are valid: "session" (traced to a
        # real session-store message) and "owner-curated" (an explicit domain query added
        # by the vault owner with no verbatim source - honestly labelled as such rather
        # than a fabricated session citation). Every query must have one or the other;
        # neither is optional.
        golden = bench.load_golden()
        provenance = json.loads((bench.GOLDEN.parent / "provenance.json").read_text(encoding="utf-8"))
        entries = provenance["entries"]
        for query in golden:
            qid = query["id"]
            self.assertIn(qid, entries, f"{qid} has no provenance record")
            record = entries[qid]
            ptype = record.get("provenance_type")
            self.assertIn(ptype, ("session", "owner-curated"),
                          f"{qid} has an invalid provenance_type {ptype!r}")
            if ptype == "session":
                for field in ("session_id", "turn_index", "timestamp", "source_quote"):
                    self.assertIn(field, record, f"{qid} session provenance missing {field!r}")
                    self.assertTrue(record[field] not in (None, ""),
                                    f"{qid} session provenance has empty {field!r}")
            else:
                self.assertTrue(record.get("note"), f"{qid} owner-curated provenance has no note")

    def test_compare_subtracts_after_from_before(self):
        report_a = {"paths": {"lexical_v0": {"status": "ok", "overall": {
            "recall_at_5": .2, "recall_at_10": .3, "recall_at_20": .4,
            "mrr": .1, "p50_ms": 10, "p95_ms": 20},
            "classes": {"paraphrase": {"recall_at_5": 0, "recall_at_10": .1,
                                       "recall_at_20": .2, "mrr": 0,
                                       "p50_ms": 11, "p95_ms": 21}}}}}
        report_b = {"paths": {"lexical_v0": {"status": "ok", "overall": {
            "recall_at_5": .5, "recall_at_10": .6, "recall_at_20": .7,
            "mrr": .4, "p50_ms": 8, "p95_ms": 25},
            "classes": {"paraphrase": {"recall_at_5": .25, "recall_at_10": .35,
                                       "recall_at_20": .45, "mrr": .5,
                                       "p50_ms": 9, "p95_ms": 19}}}}}
        rows = {row["class"]: row for row in bench.compare(report_a, report_b)}
        self.assertAlmostEqual(.3, rows["all"]["recall_at_5"])
        self.assertAlmostEqual(.3, rows["all"]["recall_at_10"])
        self.assertAlmostEqual(.3, rows["all"]["recall_at_20"])
        self.assertAlmostEqual(.3, rows["all"]["mrr"])
        self.assertAlmostEqual(-2, rows["all"]["p50_ms"])
        self.assertAlmostEqual(5, rows["all"]["p95_ms"])
        self.assertAlmostEqual(.25, rows["paraphrase"]["recall_at_5"])

    def test_compare_oracle_reports_metrics_and_pool_membership(self):
        def oracle(mrr, misses):
            def recall(hits):
                return {"hits": hits, "queries": 3, "rate": hits / 3,
                        "percentage": hits * 100 / 3}

            return {
                "overall": {
                    "recall_at_5": recall(1), "recall_at_10": recall(2),
                    "recall_at_20": recall(2), "mrr": mrr,
                },
                "classes": {
                    "local": {
                        "recall_at_5": recall(1), "recall_at_10": recall(1),
                        "recall_at_20": recall(1), "mrr": mrr,
                    },
                    "paraphrase": {
                        "recall_at_5": recall(0), "recall_at_10": recall(1),
                        "recall_at_20": recall(1), "mrr": 0.0,
                    },
                    "multihop": {
                        "recall_at_5": recall(0), "recall_at_10": recall(0),
                        "recall_at_20": recall(0), "mrr": 0.0,
                    },
                },
                "misses": [
                    {"id": identifier, "expected_pages": ["Answer"],
                     "actual_top20": []}
                    for identifier in misses
                ],
            }

        before = oracle(.2, ["g03", "g17"])
        after = oracle(.4, ["g17", "g19"])
        result = bench.compare_oracle_reports(before, after)

        rows = {row["class"]: row for row in result["delta"]}
        self.assertAlmostEqual(.0, rows["all"]["recall_at_5"])
        self.assertAlmostEqual(0.0, rows["all"]["recall_at_10"])
        self.assertAlmostEqual(0.0, rows["all"]["recall_at_20"])
        self.assertAlmostEqual(.2, rows["all"]["mrr"])
        self.assertEqual(["g03"], result["pool_membership"]["entered"])
        self.assertEqual(["g19"], result["pool_membership"]["left"])


if __name__ == "__main__":
    unittest.main()
