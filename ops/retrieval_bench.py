#!/usr/bin/env python3
"""Benchmark retrieval over the vault and long-term-memory store.

``lexical_v0`` and ``memory_v0`` deliberately duplicate the 2026-09-02
algorithms.  They are frozen reference implementations, not dead code: calling
the live MCP implementation for a baseline would silently rewrite history when
that implementation improves.  The ``*_current`` strategies import the live
module separately, so a later run can measure the delta.

Latency is warm-cache latency.  Vault text is loaded before timing, and the
live vault cache is primed with a throwaway query before measurements begin.
Recall@5 is a hit rate: a query succeeds when any expected page/fact occurs in
its first five results.  MRR uses the rank of its first correct result.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = ROOT / "wiki"
if str(WIKI_DIR) not in sys.path:
    sys.path.insert(0, str(WIKI_DIR))
import config

GOLDEN = ROOT / "tests" / "golden" / "queries.json"
CLASSES = ("local", "global", "multihop", "paraphrase")


class BenchError(ValueError):
    """Raised for an invalid golden set or an unavailable retrieval strategy."""


@dataclass(frozen=True)
class Page:
    stem: str
    text: str


def _read(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _pages(vault: Path) -> list[Page]:
    return [
        Page(path.stem, _read(path))
        for path in vault.rglob("*.md")
        if ".obsidian" not in path.parts
    ]


def resolve_page(name: str, vault: Path) -> Path | None:
    """Resolve a page stem/path with vault_mcp._resolve's behavior."""
    name = name.strip().strip("[]")
    direct = vault / name
    if direct.is_file():
        return direct
    if not name.endswith(".md"):
        direct = vault / f"{name}.md"
        if direct.is_file():
            return direct
    target = name[:-3] if name.endswith(".md") else name
    for page in vault.rglob("*.md"):
        if ".obsidian" not in page.parts and page.stem.casefold() == target.casefold():
            return page
    return None


def load_golden(path: Path = GOLDEN, vault: Path | None = None) -> list[dict]:
    """Load and validate the intentionally small, human-reviewed golden set."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"cannot read golden set {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        raise BenchError("golden set must be an object containing a queries list")
    queries = raw["queries"]
    if len(queries) < 25:
        raise BenchError("golden set must contain at least 25 queries")
    ids = set()
    paraphrases = multihop = 0
    expected_keys = {"id", "query", "expect_pages", "expect_facts", "class"}
    for entry in queries:
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise BenchError("every golden query must have exactly id, query, "
                             "expect_pages, expect_facts, class")
        if (not isinstance(entry["id"], str) or not entry["id"]
                or entry["id"] in ids or not isinstance(entry["query"], str)
                or not entry["query"].strip()
                or not isinstance(entry["expect_pages"], list)
                or not isinstance(entry["expect_facts"], list)
                or entry["class"] not in CLASSES):
            raise BenchError(f"invalid golden query {entry.get('id')!r}")
        if not entry["expect_pages"] and not entry["expect_facts"]:
            raise BenchError(f"golden query {entry['id']} has no expectation")
        if not all(isinstance(x, str) and x for x in entry["expect_pages"]):
            raise BenchError(f"golden query {entry['id']} has invalid page expectation")
        if not all(isinstance(x, str) and x for x in entry["expect_facts"]):
            raise BenchError(f"golden query {entry['id']} has invalid fact expectation")
        ids.add(entry["id"])
        paraphrases += entry["class"] == "paraphrase"
        multihop += entry["class"] == "multihop"
    if paraphrases < 8 or multihop < 3:
        raise BenchError("golden set requires at least 8 paraphrase and 3 multihop queries")
    if vault is not None:
        missing = [
            page for entry in queries for page in entry["expect_pages"]
            if resolve_page(page, vault) is None
        ]
        if missing:
            raise BenchError("unresolved expected pages: " + ", ".join(missing))
    return queries


def lexical_v0(query: str, pages: Iterable[Page], limit: int = 10) -> list[str]:
    """Frozen 2026-09-02 vault_mcp.t_search scoring and ordering."""
    terms = [term.lower() for term in query.split() if term]
    hits = []
    for page in pages:
        text_lower = page.text.lower()
        if not all(term in text_lower for term in terms):
            continue
        score = sum(text_lower.count(term) for term in terms)
        if all(term in page.stem.lower() for term in terms):
            score += 25
        hits.append((score, page.stem))
    hits.sort(key=lambda hit: -hit[0])
    return [stem for _, stem in hits[:limit]]


def sanitise_fts_v0(query: str) -> str:
    """Frozen copy of vault_mcp._sanitise_fts as of 2026-09-02."""
    import re
    parts = []
    for token in query.split():
        if token.upper() in ("AND", "OR", "NOT") or re.fullmatch(r"\w+\*?", token):
            parts.append(token)
        else:
            parts.append('"' + token.replace('"', '""') + '"')
    return " ".join(parts)


def memory_v0(query: str, db: Path, limit: int = 15) -> list[str]:
    """Frozen FTS5 ordering used by the 2026-09-02 memory MCP search."""
    sql = """
        SELECT f.content
        FROM memory_fts m
        JOIN facts f ON f.id = m.row_id AND m.table_name = 'facts'
        WHERE memory_fts MATCH ?
          AND COALESCE(f.status, 'active') = 'active'
        ORDER BY f.importance DESC, f.created_at DESC
        LIMIT ?
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        try:
            return [row[0] for row in con.execute(sql, (query, limit))]
        except sqlite3.OperationalError:
            return [row[0] for row in con.execute(sql, (sanitise_fts_v0(query), limit))]
    finally:
        con.close()


def _current_vault(query: str, _pages_: Iterable[Page], _db: Path, limit: int) -> list[str]:
    try:
        live = importlib.import_module("vault_mcp")
        text = live.t_search({"query": query, "limit": limit})
    except Exception as exc:
        raise BenchError(str(exc)) from exc
    return [line.split("]", 1)[1].strip() for line in text.splitlines()
            if line.startswith("[") and "]" in line][:limit]


def _current_memory(query: str, _pages_: Iterable[Page], _db: Path, limit: int) -> list[str]:
    try:
        live = importlib.import_module("vault_mcp")
        text = live.t_memory_search({"query": query, "limit": limit})
    except Exception as exc:
        raise BenchError(str(exc)) from exc
    return [line.split(") ", 1)[1] for line in text.splitlines()
            if line.startswith("- (") and ") " in line][:limit]


def _lexical(query: str, pages: Iterable[Page], _db: Path, limit: int) -> list[str]:
    return lexical_v0(query, pages, limit)


def _memory(query: str, _pages_: Iterable[Page], db: Path, limit: int) -> list[str]:
    return memory_v0(query, db, limit)


STRATEGIES: dict[str, Callable[[str, Iterable[Page], Path, int], list[str]]] = {
    "lexical_v0": _lexical,
    "memory_v0": _memory,
    "vault_current": _current_vault,
    "memory_current": _current_memory,
}


def _matches(result: str, entry: dict) -> bool:
    return (any(result.casefold() == page.casefold() for page in entry["expect_pages"])
            or any(result == fact for fact in entry["expect_facts"]))


def metrics(entries: list[dict], ranked: list[list[str]], latencies_ms: list[float]) -> dict:
    """Compute hit recall@5, MRR (first correct result), and latency percentiles."""
    if len(entries) != len(ranked) or len(entries) != len(latencies_ms):
        raise ValueError("entries, ranked results, and latencies must align")
    reciprocal_ranks = []
    hits = 0
    for entry, results in zip(entries, ranked):
        rank = next((i for i, value in enumerate(results, 1) if _matches(value, entry)), None)
        hits += rank is not None and rank <= 5
        reciprocal_ranks.append(0.0 if rank is None or rank > 5 else 1.0 / rank)
    ordered = sorted(latencies_ms)
    percentile = lambda p: ordered[max(0, math.ceil(len(ordered) * p) - 1)]
    return {
        "queries": len(entries),
        "recall_at_5": hits / len(entries) if entries else 0.0,
        "mrr": sum(reciprocal_ranks) / len(entries) if entries else 0.0,
        "p50_ms": statistics.median(ordered) if ordered else 0.0,
        "p95_ms": percentile(.95) if ordered else 0.0,
    }


def run(paths: list[str]) -> dict:
    resolved = config.paths()
    golden = load_golden(vault=resolved["vault"])
    page_cache = _pages(resolved["vault"])  # Warm page data before timing.
    output = {
        "format": 1,
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latency_mode": "warm",
        "recall_definition": "hit recall@5: at least one expected target in the first five",
        "mrr_definition": "mean reciprocal rank of the first correct result",
        "golden_queries": len(golden),
        "paths": {},
    }
    for name in paths:
        strategy = STRATEGIES[name]
        if name.endswith("_current") and name.startswith("vault"):
            try:
                _current_vault("__retrieval_bench_warmup__", page_cache, resolved["memory_db"], 1)
            except BenchError:
                pass
        ranked, latencies = [], []
        try:
            for entry in golden:
                start = time.perf_counter()
                ranked.append(strategy(entry["query"], page_cache, resolved["memory_db"], 10))
                latencies.append((time.perf_counter() - start) * 1000)
        except (BenchError, OSError, sqlite3.Error) as exc:
            output["paths"][name] = {"status": "unavailable", "reason": str(exc)}
            continue
        all_metrics = metrics(golden, ranked, latencies)
        by_class = {}
        for cls in CLASSES:
            indices = [i for i, entry in enumerate(golden) if entry["class"] == cls]
            if indices:
                by_class[cls] = metrics(
                    [golden[i] for i in indices], [ranked[i] for i in indices],
                    [latencies[i] for i in indices],
                )
        output["paths"][name] = {"status": "ok", "overall": all_metrics, "classes": by_class}
    return output


def _format_number(value: float, signed: bool = False) -> str:
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def _rounded_for_json(value):
    """Round only serialized floats; internal metrics retain their precision."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _rounded_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded_for_json(item) for item in value]
    return value


def print_report(report: dict) -> None:
    print(f"warm-cache retrieval benchmark ({report['golden_queries']} queries; "
          f"{report['recall_definition']})")
    print(f"{'path/class':<29} {'recall@5':>10} {'MRR':>8} {'p50 ms':>10} {'p95 ms':>10}")
    print("-" * 71)
    for name, result in report["paths"].items():
        if result["status"] != "ok":
            print(f"{name:<29} unavailable: {result['reason']}")
            continue
        for label, value in [("all", result["overall"]), *result["classes"].items()]:
            print(f"{name + '/' + label:<29} {_format_number(value['recall_at_5']):>10} "
                  f"{_format_number(value['mrr']):>8} {value['p50_ms']:>9.2f} "
                  f"{value['p95_ms']:>9.2f}")


def compare(a: dict, b: dict) -> list[dict]:
    """Return B minus A for every path/class metric shared by two reports."""
    rows = []
    for name in sorted(set(a.get("paths", {})) & set(b.get("paths", {}))):
        left, right = a["paths"][name], b["paths"][name]
        if left.get("status") != "ok" or right.get("status") != "ok":
            continue
        for cls in ("overall", *CLASSES):
            lvalue = left["overall"] if cls == "overall" else left["classes"].get(cls)
            rvalue = right["overall"] if cls == "overall" else right["classes"].get(cls)
            if not lvalue or not rvalue:
                continue
            rows.append({
                "path": name, "class": "all" if cls == "overall" else cls,
                "recall_at_5": rvalue["recall_at_5"] - lvalue["recall_at_5"],
                "mrr": rvalue["mrr"] - lvalue["mrr"],
                "p50_ms": rvalue["p50_ms"] - lvalue["p50_ms"],
                "p95_ms": rvalue["p95_ms"] - lvalue["p95_ms"],
            })
    return rows


def print_comparison(rows: list[dict]) -> None:
    print(f"{'path/class':<29} {'d recall@5':>11} {'d MRR':>8} {'d p50 ms':>10} {'d p95 ms':>10}")
    print("-" * 73)
    for row in rows:
        print(f"{row['path'] + '/' + row['class']:<29} "
              f"{_format_number(row['recall_at_5'], True):>11} "
              f"{_format_number(row['mrr'], True):>8} {row['p50_ms']:+9.2f} "
              f"{row['p95_ms']:+9.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline", metavar="OUT.json", type=Path)
    group.add_argument("--compare", metavar=("A.json", "B.json"), nargs=2, type=Path)
    parser.add_argument("--paths", help="comma-separated strategy names")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)
    if args.compare:
        try:
            before, after = (json.loads(path.read_text(encoding="utf-8")) for path in args.compare)
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read comparison input: {exc}")
        result = compare(before, after)
        if args.json:
            print(json.dumps(_rounded_for_json(result), indent=2))
        else:
            print_comparison(result)
        return 0
    names = args.paths.split(",") if args.paths else list(STRATEGIES)
    unknown = [name for name in names if name not in STRATEGIES]
    if unknown:
        parser.error("unknown retrieval paths: " + ", ".join(unknown))
    try:
        report = run(names)
    except BenchError as exc:
        print(f"retrieval benchmark error: {exc}", file=sys.stderr)
        return 1
    args.baseline.parent.mkdir(parents=True, exist_ok=True)
    serialized = _rounded_for_json(report)
    args.baseline.write_text(json.dumps(serialized, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(serialized, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
