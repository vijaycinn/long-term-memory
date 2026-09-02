"""Dependency-free second-stage ranking for hybrid vault results.

The vector index retrieves chunks, but the vault returns pages.  Aggregating
the already-indexed chunk vectors per candidate restores that distinction
without another embedding request or a second vector-store schema.
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
from pathlib import Path

import config

# RRF earned its place as the high-recall first stage, so it remains the largest
# individual signal rather than allowing a heuristic to manufacture relevance.
RRF_WEIGHT = 0.60
# A page title is hand-authored navigation metadata; it is much less likely than
# a body mention to be incidental, hence the strongest precision correction.
TITLE_COVERAGE_WEIGHT = 0.20
# This uses a centroid of precomputed page chunks, not the one lucky chunk that
# retrieved the page.  It is bounded so semantic similarity cannot erase RRF.
PAGE_SEMANTIC_WEIGHT = 0.08
# The vault's observed taxonomy separates canonical technical/topic notes from
# activity, meta, and people records, but taxonomy must not dominate relevance.
PATH_CLASS_WEIGHT = 0.07
# Agreement by independent lexical and dense retrievers is modest corroboration,
# already partly represented in RRF, so this deliberately stays small.
FUSION_WEIGHT = 0.03
# Long pages have more chunks and more first-stage chances.  A small, bounded
# correction avoids making terse stubs win merely for being short.
LENGTH_NORMALISATION_WEIGHT = 0.02
# Taxonomy is a tie-breaker, not evidence that an arbitrary page answers a
# paraphrase.  Require a substantial title signal before it may alter RRF.
MIN_TITLE_COVERAGE = 0.50

_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "did", "do",
    "does", "for", "from", "how", "i", "in", "is", "it", "me", "my", "of",
    "on", "or", "our", "should", "that", "the", "to", "was", "we", "what",
    "when", "which", "who", "why", "with", "would", "you", "your",
})


def _terms(text: str) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(text)
            if word.casefold() not in _STOPWORDS}


def _taxonomy(vault: Path) -> dict[str, set[str]]:
    """Discover the installed vault's classes instead of assuming its layout."""
    roots = {p.name.casefold() for p in vault.iterdir() if p.is_dir()}
    return {
        "specific": {name for name in roots if "tech" in name or "knowledge" in name},
        "generic": {name for name in roots
                    if any(token in name for token in ("meta", "activity"))},
    }


def _path_prior(path: str, taxonomy: dict[str, set[str]]) -> float:
    parts = Path(path).parts
    root = parts[0].casefold() if parts else ""
    rest = " ".join(part.casefold() for part in parts[1:])
    name = Path(path).stem.casefold()
    if (root in taxonomy["generic"] or re.search(r"\b(?:moc|index|log)\b", name)
            or ("people" in root and "internal" in rest)):
        return 0.0
    if "people" in root and "accounts" in rest:
        return 0.68
    if root in taxonomy["specific"] and ("tech" in root or "topics" in rest):
        return 1.0
    if root in taxonomy["specific"]:
        return 0.72
    if "research" in root or "synthesis" in root:
        return 0.72
    if "outputs" in root:
        return 0.56
    return 0.42


def _cosine(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else None


_SEMANTIC_STATE: dict[str, tuple[int, dict[str, list[float]], dict[str, int],
                                 dict[str, list[float]]]] = {}


def _page_semantics(query: str, candidates: list[dict], db: Path) -> tuple[dict[str, float], dict[str, int]]:
    """Return page-centroid cosine scores from only cached, precomputed vectors."""
    import vault_vectors

    key = str(db.resolve())
    state = _SEMANTIC_STATE.get(key)
    if state is None:
        state = _load_semantic_state(vault_vectors, db)
        if state is None:
            return {}, {}
        _SEMANTIC_STATE[key] = state
    dims, centroids, counts, queries = state
    query_key = vault_vectors._hash(f"{dims}\x00{query}")
    query_vector = queries.get(query_key)
    if query_vector is None:
        query_vector = _cached_query(db, query_key)
        if query_vector is not None:
            queries[query_key] = query_vector
    if query_vector is None:
        return {}, {}
    scores = {}
    for candidate in candidates:
        path = candidate["path"]
        centroid = centroids.get(path)
        score = _cosine(query_vector, centroid) if centroid else None
        if score is not None:
            scores[path] = max(0.0, (score + 1.0) / 2.0)
    return scores, {row["path"]: counts.get(row["path"], 1) for row in candidates}


def _cached_query(db: Path, key: str) -> list[float] | None:
    """Read hybrid's freshly cached query vector without loading sqlite-vec again."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT embedding FROM query_cache WHERE key = ?", (key,)).fetchone()
        con.close()
        if not row:
            return None
        blob = row[0]
        return list(struct.unpack(f"{len(blob) // 4}f", blob))
    except (sqlite3.Error, OSError, struct.error):
        return None


def _load_semantic_state(vault_vectors, db: Path):
    """Load immutable page centroids once per MCP process, not once per search."""
    con = vault_vectors._connect(db)
    if con is None:
        return None
    try:
        dims = vault_vectors._stored_dimensions(con)
        if dims is None:
            return None
        rows = con.execute(
            "SELECT c.path, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.rowid"
        ).fetchall()
        query_rows = con.execute("SELECT key, embedding FROM query_cache").fetchall()
    except Exception:
        return None
    finally:
        con.close()

    vectors: dict[str, list[list[float]]] = {}
    for row in rows:
        try:
            vectors.setdefault(row["path"], []).append(list(struct.unpack(
                f"{len(row['embedding']) // 4}f", row["embedding"])))
        except (KeyError, TypeError, struct.error):
            continue
    centroids: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for path, chunks in vectors.items():
        counts[path] = len(chunks)
        centroids[path] = [sum(values) / len(chunks) for values in zip(*chunks)]
    queries: dict[str, list[float]] = {}
    for row in query_rows:
        try:
            blob = row["embedding"]
            queries[row["key"]] = list(struct.unpack(f"{len(blob) // 4}f", blob))
        except (KeyError, TypeError, struct.error):
            continue
    return dims, centroids, counts, queries


def _rank(query: str, candidates: list[dict], vault: Path,
          semantics: dict[str, float], chunk_counts: dict[str, int]) -> list[dict]:
    taxonomy = _taxonomy(vault)
    query_terms = _terms(query)
    max_rrf = max((float(row.get("rrf", 0.0)) for row in candidates), default=1.0) or 1.0
    ranked: list[dict] = []
    for row in candidates:
        result = dict(row)
        path = str(result["path"])
        stem_terms = _terms(Path(path).stem)
        title_coverage = (len(query_terms & stem_terms) / len(query_terms)
                          if query_terms else 0.0)
        rrf = float(result.get("rrf", 0.0)) / max_rrf
        fusion = float(any(source.startswith("bm25#") for source in result.get("sources", ()))
                       and any(source.startswith("vector#") for source in result.get("sources", ())))
        count = chunk_counts.get(path, 1)
        length = 1.0 / (1.0 + math.log2(max(1, count)))
        semantic = semantics.get(path, 0.0)
        result["rerank_score"] = (
            RRF_WEIGHT * rrf
            + TITLE_COVERAGE_WEIGHT * title_coverage
            + PAGE_SEMANTIC_WEIGHT * semantic
            + PATH_CLASS_WEIGHT * _path_prior(path, taxonomy)
            + FUSION_WEIGHT * fusion
            + LENGTH_NORMALISATION_WEIGHT * length
        )
        ranked.append(result)
    # Path is stable across runs and survives equal floating point scores.
    return sorted(ranked, key=lambda row: (-row["rerank_score"], row["path"].casefold()))


def rerank(query: str, candidates: list[dict], limit: int = 5,
           vault: Path | None = None, db: Path | None = None) -> list[dict]:
    """Rerank a fused candidate pool, degrading to its existing order on failure."""
    if not candidates:
        return []
    vault = Path(vault or config.paths()["vault"])
    db = Path(db or config.paths()["vault_vectors"])
    try:
        query_terms = _terms(query)
        if not query_terms or not any(
                len(query_terms & _terms(Path(row["path"]).stem)) / len(query_terms)
                >= MIN_TITLE_COVERAGE for row in candidates):
            return candidates[:limit]
        semantics, chunk_counts = _page_semantics(query, candidates, db)
        return _rank(query, candidates, vault, semantics, chunk_counts)[:limit]
    except Exception:
        return candidates[:limit]
