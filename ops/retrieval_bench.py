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
Oracle mode measures page-only recall at ranks 5, 10, and 20 from one fused
candidate pool per query.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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
ORACLE_CLASSES = ("local", "paraphrase", "multihop")
ORACLE_LIMIT = 20
BENCH_LIMIT = 20
ORACLE_ARTIFACT = ROOT / "docs" / "bench" / "oracle-ceiling-2026-09-02.json"
_RANKED_RESULT_RE = re.compile(r"^\s*\[[^\]\r\n]+\]\s+(?P<stem>\S.*)$")
_MEMORY_RESULT_RE = re.compile(r"^-\s+\([^)]+\)(?:\s+\[[^\]\r\n]+\])?\s+(?P<content>\S.*)$")
QUALITY_METRICS = ("recall_at_5", "recall_at_10", "recall_at_20", "mrr")
_EXPLICIT_DB_NOTE = (
    "Explicit sidecars are copied to a temporary evaluation DB so query_cache "
    "writes never modify the measured source sidecar."
)


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


def _resolve_vectors_db(vectors_db: Path | None) -> Path:
    override = str(vectors_db.expanduser()) if vectors_db is not None else None
    return config.paths(vault_vectors=override)["vault_vectors"]


def _index_provenance(db: Path, require_exists: bool = False) -> dict:
    try:
        import vault_vectors
    except ImportError as exc:
        raise BenchError(f"cannot import vault_vectors: {exc}") from exc
    try:
        status = vault_vectors.status(db=db)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BenchError(f"cannot read vector sidecar status: {exc}") from exc
    if require_exists and not status.get("exists"):
        raise BenchError(f"vector sidecar does not exist: {db}")

    dimensions = status.get("dimensions")
    try:
        dimensions = int(dimensions) if dimensions is not None else None
    except (TypeError, ValueError):
        pass
    return {
        "path": str(status.get("db", db)),
        "exists": bool(status.get("exists")),
        "chunks": int(status.get("chunks", 0)),
        "pages": int(status.get("pages", 0)),
        "dimensions": dimensions,
        "model": status.get("model"),
    }


@contextmanager
def _evaluation_db(db: Path, isolated: bool):
    if not isolated:
        yield db
        return
    try:
        with tempfile.TemporaryDirectory(prefix="retrieval-bench-") as temp_dir:
            evaluation_db = Path(temp_dir) / db.name
            shutil.copy2(db, evaluation_db)
            yield evaluation_db
    except (OSError, shutil.Error) as exc:
        raise BenchError(f"cannot create read-only evaluation copy: {exc}") from exc


def _current_vault_selected(
    query: str, pages: Iterable[Page], limit: int, vectors_db: Path,
) -> list[str]:
    try:
        import vault_vectors
        pool = vault_vectors.search_hybrid(
            query, max(limit, ORACLE_LIMIT), db=vectors_db,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BenchError(f"vector search failed: {exc}") from exc

    if not pool:
        return lexical_v0(query, pages, limit)
    try:
        import vault_rerank
    except ImportError:
        rows = pool[:limit]
    else:
        rows = vault_rerank.rerank(query, pool, limit, db=vectors_db)
    return [Path(row["path"]).stem for row in rows[:limit]]


def _current_vault(
    query: str, pages: Iterable[Page], _db: Path, limit: int,
    vectors_db: Path | None = None,
) -> list[str]:
    if vectors_db is not None:
        return _current_vault_selected(query, pages, limit, vectors_db)
    try:
        live = importlib.import_module("vault_mcp")
        text = live.t_search({"query": query, "limit": limit})
    except Exception as exc:
        raise BenchError(str(exc)) from exc
    return [
        match.group("stem")
        for line in text.splitlines()
        if (match := _RANKED_RESULT_RE.match(line))
    ][:limit]


def _current_memory(query: str, _pages_: Iterable[Page], _db: Path, limit: int) -> list[str]:
    try:
        live = importlib.import_module("vault_mcp")
        text = live.t_memory_search({"query": query, "limit": limit})
    except Exception as exc:
        raise BenchError(str(exc)) from exc
    return [
        match.group("content")
        for line in text.splitlines()
        if (match := _MEMORY_RESULT_RE.match(line))
    ][:limit]


def _current_vault_hops1(
    query: str, pages: Iterable[Page], _db: Path, limit: int,
    vectors_db: Path | None = None,
) -> list[str]:
    """Expand outbound neighbours for the top three live vault results.

    Capping direct retrieval at three leaves ranked positions for one-hop
    candidates; fetching ten direct results first would make graph expansion
    invisible to Recall@5 while paying the same traversal cost.
    """
    try:
        live = importlib.import_module("vault_mcp")
        if vectors_db is None:
            direct_text = live.t_search({"query": query, "limit": min(limit, 3)})
            direct = [
                match.group("stem")
                for line in direct_text.splitlines()
                if (match := _RANKED_RESULT_RE.match(line))
            ]
        else:
            direct = _current_vault(query, pages, _db, min(limit, 3), vectors_db)
        ranked = list(direct)
        seen = {stem.casefold() for stem in ranked}
        for stem in direct[:3]:
            page = live._resolve(stem)
            if page is None:
                continue
            text = live.read(page)
            for neighbour, _summary, _kind in live._neighbours(text):
                if neighbour.casefold() not in seen:
                    seen.add(neighbour.casefold())
                    ranked.append(neighbour)
                    if len(ranked) >= limit:
                        return ranked
        return ranked[:limit]
    except Exception as exc:
        raise BenchError(str(exc)) from exc


def _lexical(query: str, pages: Iterable[Page], _db: Path, limit: int) -> list[str]:
    return lexical_v0(query, pages, limit)


def _memory(query: str, _pages_: Iterable[Page], db: Path, limit: int) -> list[str]:
    return memory_v0(query, db, limit)


STRATEGIES: dict[str, Callable[[str, Iterable[Page], Path, int], list[str]]] = {
    "lexical_v0": _lexical,
    "memory_v0": _memory,
    "vault_current": _current_vault,
    "vault_current_hops1": _current_vault_hops1,
    "memory_current": _current_memory,
}


def _matches(result: str, entry: dict) -> bool:
    return (any(result.casefold() == page.casefold() for page in entry["expect_pages"])
            or any(result == fact for fact in entry["expect_facts"]))


def metrics(entries: list[dict], ranked: list[list[str]], latencies_ms: list[float]) -> dict:
    """Compute hit recall at three cutoffs, MRR, and latency percentiles."""
    if len(entries) != len(ranked) or len(entries) != len(latencies_ms):
        raise ValueError("entries, ranked results, and latencies must align")
    ranks = [
        next((i for i, value in enumerate(results, 1) if _matches(value, entry)), None)
        for entry, results in zip(entries, ranked)
    ]
    hits = {
        cutoff: sum(rank is not None and rank <= cutoff for rank in ranks)
        for cutoff in (5, 10, 20)
    }
    reciprocal_ranks = [
        0.0 if rank is None or rank > 5 else 1.0 / rank
        for rank in ranks
    ]
    ordered = sorted(latencies_ms)
    percentile = lambda p: ordered[max(0, math.ceil(len(ordered) * p) - 1)]
    return {
        "queries": len(entries),
        "recall_at_5": hits[5] / len(entries) if entries else 0.0,
        "recall_at_10": hits[10] / len(entries) if entries else 0.0,
        "recall_at_20": hits[20] / len(entries) if entries else 0.0,
        "mrr": sum(reciprocal_ranks) / len(entries) if entries else 0.0,
        "p50_ms": statistics.median(ordered) if ordered else 0.0,
        "p95_ms": percentile(.95) if ordered else 0.0,
    }


def _oracle_page_stem(value: str) -> str:
    name = Path(value).name
    if name.casefold().endswith(".md"):
        name = name[:-3]
    return name.casefold()


def _oracle_rank(entry: dict, pool: list[dict], limit: int = ORACLE_LIMIT) -> int | None:
    expected = {_oracle_page_stem(page) for page in entry["expect_pages"]}
    for rank, result in enumerate(pool[:limit], 1):
        path = result.get("path")
        if isinstance(path, str) and _oracle_page_stem(path) in expected:
            return rank
    return None


def _recall_stat(hits: int, total: int) -> dict:
    return {
        "hits": hits,
        "queries": total,
        "rate": hits / total if total else 0.0,
        "percentage": hits * 100 / total if total else 0.0,
    }


def oracle_metrics(entries: list[dict], pools: list[list[dict]]) -> dict:
    """Compute page-only recall at each candidate-pool cutoff."""
    if len(entries) != len(pools):
        raise ValueError("entries and candidate pools must align")
    ranks = [_oracle_rank(entry, pool) for entry, pool in zip(entries, pools)]
    total = len(entries)
    hits = {cutoff: sum(rank is not None and rank <= cutoff for rank in ranks)
            for cutoff in (5, 10, ORACLE_LIMIT)}
    headroom_hits = hits[ORACLE_LIMIT] - hits[5]
    return {
        "queries": total,
        "recall_at_5": _recall_stat(hits[5], total),
        "recall_at_10": _recall_stat(hits[10], total),
        "recall_at_20": _recall_stat(hits[ORACLE_LIMIT], total),
        "mrr": (
            sum(1.0 / rank for rank in ranks if rank is not None and rank <= 5) / total
            if total else 0.0
        ),
        "reranker_headroom": {
            "additional_hits": headroom_hits,
            "queries": total,
            "rate": headroom_hits / total if total else 0.0,
            "percentage_points": headroom_hits * 100 / total if total else 0.0,
        },
    }


def _oracle_candidates(pool: list[dict]) -> list[dict]:
    candidates = []
    for rank, result in enumerate(pool[:ORACLE_LIMIT], 1):
        path = result.get("path")
        candidate = {"rank": rank, "path": str(path) if path is not None else ""}
        sources = result.get("sources")
        if isinstance(sources, list):
            candidate["sources"] = [str(source) for source in sources]
        candidates.append(candidate)
    return candidates


def _oracle_misses(entries: list[dict], pools: list[list[dict]]) -> list[dict]:
    misses = []
    for entry, pool in zip(entries, pools):
        if _oracle_rank(entry, pool) is not None:
            continue
        misses.append({
            "id": entry["id"],
            "query": entry["query"],
            "expected_pages": list(entry["expect_pages"]),
            "actual_top20": _oracle_candidates(pool),
        })
    return misses


def _git_provenance() -> dict:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BenchError(f"cannot capture git provenance: {exc}") from exc
    return {"revision": revision, "diff_stat": diff_stat}


def run_oracle(vectors_db: Path | None = None) -> dict:
    """Measure the fused candidate ceiling with one hybrid call per query."""
    resolved = config.paths()
    golden = load_golden(vault=resolved["vault"])
    selected_db = _resolve_vectors_db(vectors_db)
    index_provenance = _index_provenance(
        selected_db, require_exists=vectors_db is not None,
    )
    try:
        import vault_vectors
    except ImportError as exc:
        raise BenchError(f"cannot import vault_vectors: {exc}") from exc

    with _evaluation_db(selected_db, vectors_db is not None) as search_db:
        db_argument = search_db if vectors_db is not None else None
        pools = []
        try:
            for entry in golden:
                pools.append(vault_vectors.search_hybrid(
                    entry["query"], ORACLE_LIMIT, db=db_argument,
                ))
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BenchError(f"candidate-pool search failed: {exc}") from exc

        overall = oracle_metrics(golden, pools)
        by_class = {}
        for cls in ORACLE_CLASSES:
            indices = [i for i, entry in enumerate(golden) if entry["class"] == cls]
            by_class[cls] = oracle_metrics(
                [golden[i] for i in indices],
                [pools[i] for i in indices],
            )
        report = {
            "format": 1,
            "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "retrieval": "vault_vectors.search_hybrid(query, 20)",
            "pool_size": ORACLE_LIMIT,
            "recall_definition": (
                "page-only hit recall at the first five, ten, and twenty fused candidates"
            ),
            "mrr_definition": (
                "mean reciprocal rank of the first expected page within the first five results"
            ),
            "expectations_used": "expect_pages only; expect_facts are intentionally ignored",
            "golden_queries": len(golden),
            "overall": overall,
            "classes": by_class,
            "misses": _oracle_misses(golden, pools),
            "index_provenance": index_provenance,
            "provenance": _git_provenance(),
        }
        if vectors_db is not None:
            report["evaluation_note"] = _EXPLICIT_DB_NOTE
        return report


def run(paths: list[str], vectors_db: Path | None = None) -> dict:
    resolved = config.paths()
    golden = load_golden(vault=resolved["vault"])
    page_cache = _pages(resolved["vault"])  # Warm page data before timing.
    selected_db = _resolve_vectors_db(vectors_db)
    index_provenance = _index_provenance(
        selected_db, require_exists=vectors_db is not None,
    )
    output = {
        "format": 1,
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latency_mode": "warm",
        "recall_definition": (
            "hit recall at ranks 5, 10, and 20: at least one expected target "
            "within each cutoff"
        ),
        "mrr_definition": "mean reciprocal rank of the first correct result",
        "golden_queries": len(golden),
        "index_provenance": index_provenance,
        "provenance": _git_provenance(),
        "paths": {},
    }
    with _evaluation_db(selected_db, vectors_db is not None) as search_db:
        db_argument = search_db if vectors_db is not None else None
        for name in paths:
            strategy = STRATEGIES[name]
            if name.startswith("vault_current"):
                try:
                    _current_vault(
                        "__retrieval_bench_warmup__", page_cache, resolved["memory_db"], 1,
                        db_argument,
                    )
                except BenchError:
                    pass
            ranked, latencies = [], []
            try:
                for entry in golden:
                    start = time.perf_counter()
                    if name.startswith("vault_current"):
                        ranked.append(strategy(
                            entry["query"], page_cache, resolved["memory_db"],
                            BENCH_LIMIT, db_argument,
                        ))
                    else:
                        ranked.append(strategy(
                            entry["query"], page_cache, resolved["memory_db"], BENCH_LIMIT,
                        ))
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
            output["paths"][name] = {
                "status": "ok", "overall": all_metrics, "classes": by_class,
            }
    if vectors_db is not None:
        output["evaluation_note"] = _EXPLICIT_DB_NOTE
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
    print(f"{'path/class':<29} {'recall@5':>10} {'recall@10':>10} "
          f"{'recall@20':>10} {'MRR':>8} {'p50 ms':>10} {'p95 ms':>10}")
    print("-" * 114)
    for name, result in report["paths"].items():
        if result["status"] != "ok":
            print(f"{name:<29} unavailable: {result['reason']}")
            continue
        for label, value in [("all", result["overall"]), *result["classes"].items()]:
            print(f"{name + '/' + label:<29} {_format_number(value['recall_at_5']):>10} "
                  f"{_format_number(value['recall_at_10']):>10} "
                  f"{_format_number(value['recall_at_20']):>10} "
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
            row = {"path": name, "class": "all" if cls == "overall" else cls}
            for metric in (*QUALITY_METRICS, "p50_ms", "p95_ms"):
                if metric in lvalue and metric in rvalue:
                    row[metric] = rvalue[metric] - lvalue[metric]
            rows.append(row)
    return rows


def _oracle_path_result(report: dict) -> dict:
    def flatten(value: dict) -> dict:
        return {
            "recall_at_5": value["recall_at_5"]["rate"],
            "recall_at_10": value["recall_at_10"]["rate"],
            "recall_at_20": value["recall_at_20"]["rate"],
            "mrr": value["mrr"],
        }

    return {
        "status": "ok",
        "overall": flatten(report["overall"]),
        "classes": {
            cls: flatten(report["classes"][cls])
            for cls in ORACLE_CLASSES
            if cls in report.get("classes", {})
        },
    }


def compare_oracle_reports(before: dict, after: dict) -> dict:
    """Compare two oracle runs and retain the miss evidence on both sides."""
    before_path = _oracle_path_result(before)
    after_path = _oracle_path_result(after)
    rows = compare(
        {"paths": {"vault_vectors": before_path}},
        {"paths": {"vault_vectors": after_path}},
    )
    before_misses = {miss["id"]: miss for miss in before.get("misses", [])}
    after_misses = {miss["id"]: miss for miss in after.get("misses", [])}
    return {
        "format": 1,
        "comparison": "shadow-index",
        "before": before,
        "after": after,
        "delta": rows,
        "pool_membership": {
            "entered": sorted(set(before_misses) - set(after_misses)),
            "left": sorted(set(after_misses) - set(before_misses)),
        },
        "latency_note": (
            "Each sidecar has its own query_cache. A fresh shadow sidecar can "
            "make its first run slower because query embeddings are uncached; "
            "the comparison measures retrieval quality, not cold-cache latency."
        ),
    }


def print_comparison(rows: list[dict]) -> None:
    print(f"{'path/class':<29} {'d recall@5':>11} {'d recall@10':>11} "
          f"{'d recall@20':>11} {'d MRR':>8} {'d p50 ms':>10} {'d p95 ms':>10}")
    print("-" * 120)
    for row in rows:
        quality = [
            _format_number(row[metric], True) if metric in row else "n/a"
            for metric in ("recall_at_5", "recall_at_10", "recall_at_20", "mrr")
        ]
        latency = [
            f"{row[metric]:+9.2f}" if metric in row else "      n/a"
            for metric in ("p50_ms", "p95_ms")
        ]
        print(f"{row['path'] + '/' + row['class']:<29} "
              f"{quality[0]:>11} {quality[1]:>11} {quality[2]:>11} "
              f"{quality[3]:>8} {latency[0]} {latency[1]}")


def _format_recall(value: dict) -> str:
    return f"{value['hits']}/{value['queries']} ({value['percentage']:.3f}%)"


def print_oracle_report(report: dict, artifact: Path) -> None:
    print(f"candidate-pool oracle ({report['golden_queries']} queries; top-{report['pool_size']} fused)")
    print(f"{'class':<14} {'Recall@5':>18} {'Recall@10':>18} "
          f"{'Recall@20':>18} {'MRR':>10} {'headroom':>23}")
    print("-" * 109)
    for label, value in [("all", report["overall"]), *report["classes"].items()]:
        headroom = value["reranker_headroom"]
        headroom_text = (
            f"{headroom['additional_hits']}/{headroom['queries']} "
            f"(+{headroom['percentage_points']:.3f} pp)"
        )
        print(f"{label:<14} {_format_recall(value['recall_at_5']):>18} "
              f"{_format_recall(value['recall_at_10']):>18} "
              f"{_format_recall(value['recall_at_20']):>18} "
              f"{value['mrr']:>10.4f} {headroom_text:>23}")
    print(f"\nTop-{report['pool_size']} misses ({len(report['misses'])}):")
    if not report["misses"]:
        print("none")
    for miss in report["misses"]:
        print(f"- {miss['id']}: {miss['query']}")
        print(f"  expected: {', '.join(miss['expected_pages'])}")
        print("  actual:")
        for candidate in miss["actual_top20"]:
            source_text = (
                f" [{', '.join(candidate['sources'])}]"
                if candidate.get("sources") else ""
            )
            print(f"    {candidate['rank']:>2}. {candidate['path']}{source_text}")
    print(f"\nArtifact: {artifact}")
    print(f"Measured revision: {report['provenance']['revision']}")
    print(f"Index: {report['index_provenance']['path']} "
          f"({report['index_provenance']['chunks']} chunks / "
          f"{report['index_provenance']['pages']} pages)")


def print_oracle_comparison(comparison: dict) -> None:
    before = comparison["before"]["index_provenance"]
    after = comparison["after"]["index_provenance"]
    print("shadow-index comparison (after minus before)")
    print(f"before: {before['path']} ({before['chunks']} chunks / {before['pages']} pages)")
    print(f"after:  {after['path']} ({after['chunks']} chunks / {after['pages']} pages)")
    print()
    print_comparison(comparison["delta"])
    membership = comparison["pool_membership"]
    print(f"\nEntered top-{ORACLE_LIMIT} pool: "
          f"{', '.join(membership['entered']) or 'none'}")
    print(f"Left top-{ORACLE_LIMIT} pool: "
          f"{', '.join(membership['left']) or 'none'}")

    for label in ("before", "after"):
        misses = comparison[label].get("misses", [])
        print(f"\n{label} misses ({len(misses)}):")
        if not misses:
            print("none")
            continue
        for miss in misses:
            actual = ", ".join(
                f"{candidate['rank']}:{candidate['path']}"
                for candidate in miss["actual_top20"]
            )
            print(f"- {miss['id']}: expected {', '.join(miss['expected_pages'])}")
            print(f"  actual: {actual or 'none'}")
    print(f"\n{comparison['latency_note']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline", metavar="OUT.json", type=Path)
    group.add_argument("--compare", metavar=("A.json", "B.json"), nargs=2, type=Path)
    group.add_argument("--compare-vectors", metavar=("A.db", "B.db"), nargs=2, type=Path,
                       help="run the oracle against two sidecars and compare them")
    group.add_argument("--oracle", action="store_true",
                       help="measure the fused top-20 candidate-pool ceiling")
    parser.add_argument("--paths", help="comma-separated strategy names")
    parser.add_argument("--vectors-db", metavar="PATH", type=Path,
                        help="measure vault retrieval against this sidecar")
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
    if args.compare_vectors:
        if args.vectors_db is not None:
            parser.error("--vectors-db cannot be combined with --compare-vectors")
        try:
            before = run_oracle(args.compare_vectors[0])
            after = run_oracle(args.compare_vectors[1])
            result = compare_oracle_reports(before, after)
        except (BenchError, OSError) as exc:
            print(f"retrieval benchmark error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_rounded_for_json(result), indent=2))
        else:
            print_oracle_comparison(result)
        return 0
    if args.oracle:
        try:
            report = run_oracle(args.vectors_db)
            ORACLE_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            ORACLE_ARTIFACT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except (BenchError, OSError) as exc:
            print(f"retrieval benchmark error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_oracle_report(report, ORACLE_ARTIFACT)
        return 0
    names = args.paths.split(",") if args.paths else list(STRATEGIES)
    unknown = [name for name in names if name not in STRATEGIES]
    if unknown:
        parser.error("unknown retrieval paths: " + ", ".join(unknown))
    try:
        report = run(names, args.vectors_db)
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
