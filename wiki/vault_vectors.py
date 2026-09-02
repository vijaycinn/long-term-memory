#!/usr/bin/env python3
"""Vector sidecar for the vault — semantic retrieval over page chunks.

Design decisions worth keeping, because each one was a trap:

**Sidecar, never memory.db.** memory.db is covered by the verified
backup/restore drill in ops/ltm_backup.py. Embeddings are a rebuildable
derivative; putting them there would bloat every snapshot for no recovery value.

**Keyed on content hash, not mtime.** The plan originally suggested
`(path, mtime, chunk_ix)`. That is wrong for this vault: `wiki/build.py`
regenerates pages on every rebuild, so mtime changes even when the text is
byte-identical, and the whole corpus would re-embed on a no-op rebuild. Hashing
the chunk text means a rebuild that changes nothing costs nothing — which is
also what makes a committed vector DB stay useful instead of going stale
immediately.

**Boilerplate is stripped before embedding.** An audit measured 61% of the
vault's 6,342 wikilinks as auto-generated `## Related` blocks and breadcrumbs.
Embedding those pulls every page toward the same centroid and actively degrades
semantic search. Frontmatter goes too: `generated: true` and `source:` lines
carry no meaning a query would ever match.

**Small-to-big.** Chunks are the retrieval unit; the *page* is the answer unit.
A hit on chunk 7 returns the parent page, deduplicated, scored by its best
chunk. Returning raw chunks would strand the reader mid-document.

**Brute-force KNN, deliberately.** At roughly 2-4k vectors this is sub-millisecond
and exact. Do not add an ANN index; revisit somewhere north of 100k vectors.

Degrades to nothing — not an error — when sqlite-vec is absent, the endpoint is
unconfigured, or `az login` has lapsed. Callers fall back to lexical search.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent
if str(BUILD) not in sys.path:
    sys.path.insert(0, str(BUILD))
import config
import vault_embed

# Chunking. text-embedding-3-small accepts 8191 tokens, far more than we use:
# smaller chunks retrieve more precisely, because one chunk should carry one
# idea. ~1200 chars is roughly 300 tokens of English prose.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
MIN_CHUNK_CHARS = 80          # below this a chunk is a heading fragment, not content
CONTEXT_HEADER_MAX_CHARS = 420

FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)
RELATED_BLOCK_RE = re.compile(r"\n##+ Related\b.*?(?=\n##+ |\Z)", re.S | re.I)
BREADCRUMB_RE = re.compile(r"^\s*(?:\[\[[^\]]+\]\]\s*(?:›|>|/)\s*)+\[\[[^\]]+\]\]\s*$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
CALLOUT_RE = re.compile(r"^>\s*\[![a-z]+\]\s*", re.M | re.I)
HEADING_RE = re.compile(r"^#{2,6}\s+(.+?)\s*$", re.M)


def _sqlite_vec():
    """Import sqlite-vec, or None. Absence is a supported configuration."""
    try:
        import sqlite_vec
        return sqlite_vec
    except ImportError:
        return None


def available() -> bool:
    return _sqlite_vec() is not None and bool(config.get("embed_endpoint"))


def _connect(path: Path, create: bool = False) -> sqlite3.Connection | None:
    vec = _sqlite_vec()
    if vec is None:
        return None
    if not create and not path.is_file():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.enable_load_extension(True)
        vec.load(con)
        con.enable_load_extension(False)
    except (sqlite3.Error, AttributeError, OSError) as e:
        print(f"[vault_vectors] cannot open sidecar: {e}", file=sys.stderr)
        return None
    return con


def _init(con: sqlite3.Connection, dims: int) -> None:
    con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
            embedding float[{dims}]
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            rowid        INTEGER PRIMARY KEY,
            path         TEXT NOT NULL,
            chunk_ix     INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            preview      TEXT,
            UNIQUE(path, chunk_ix)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
    # Real BM25 over the same cleaned chunk text. The vault's own search was
    # str.count() over raw page text, which is not a relevance model at all --
    # fusing a good vector score with that would waste the vector work. FTS5 is
    # already a dependency of this project, so this costs nothing new.
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            body, path UNINDEXED, chunk_ix UNINDEXED
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT
        )""")
    # Query-embedding cache. With a remote embedding API the query round-trip
    # dominates search latency (measured ~650 ms of a ~667 ms hybrid p50, against
    # 16 ms for BM25 alone). Agents repeat queries constantly -- across retries,
    # follow-ups and re-runs -- so caching by query text turns the second and
    # subsequent identical searches into pure local work.
    con.execute("""
        CREATE TABLE IF NOT EXISTS query_cache (
            key        TEXT PRIMARY KEY,
            embedding  BLOB NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
    con.commit()


# ------------------------------------------------------------- text prep
def clean_text(text: str) -> str:
    """Strip everything a query would never legitimately match."""
    text = FRONTMATTER_RE.sub("", text)
    text = RELATED_BLOCK_RE.sub("", text)
    text = BREADCRUMB_RE.sub("", text)
    text = CALLOUT_RE.sub("", text)
    # Keep the human-readable half of a wikilink; drop the bracket syntax.
    text = WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _chunk_spans(text: str, size: int = CHUNK_CHARS,
                 overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int]]:
    """Split text while retaining offsets for deterministic section context."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [(text, 0)]
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer a paragraph break, then a sentence break, then a hard cut.
            for sep in ("\n\n", "\n", ". "):
                cut = text.rfind(sep, start + size // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[start:end].strip()
        if len(piece) >= MIN_CHUNK_CHARS:
            out.append((piece, start))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def chunk(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on paragraph boundaries where possible, with overlap for context."""
    return [piece for piece, _ in _chunk_spans(text, size, overlap)]


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def _pages(vault: Path):
    for p in vault.rglob("*.md"):
        if ".obsidian" not in p.parts:
            yield p


def _read(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _plain_context(text: str, limit: int) -> str:
    text = WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)), text)
    text = re.sub(r"[#*_`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _frontmatter_summary(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return ""
    for line in match.group(0).splitlines()[1:-1]:
        key, separator, value = line.partition(":")
        if separator and key.strip().casefold() == "summary":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return _plain_context(value, 120)
    return ""


def _page_title(rel: str, text: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if match:
        title = _plain_context(match.group(1), 100)
        if title:
            return title
    return _plain_context(re.sub(r"[-_]+", " ", Path(rel).stem).title(), 100)


def _page_class(rel: str, text: str) -> str:
    parent = Path(rel).parent.as_posix()
    for line in text.splitlines():
        if BREADCRUMB_RE.fullmatch(line):
            labels = [
                _plain_context(m.group(2) or m.group(1), 48)
                for m in WIKILINK_RE.finditer(line)
            ]
            labels = [label for label in labels if label]
            if labels:
                context = labels[:-1] or labels
                if not (len(context) == 1
                        and context[0].casefold() in {"home", "index"}):
                    return " > ".join(context[-3:])[:100].rstrip()
    return parent if parent != "." else "vault"


def _nearest_heading(text: str, offset: int) -> str:
    headings = [m.group(1) for m in HEADING_RE.finditer(text)
                if m.start() <= offset]
    return _plain_context(headings[-1], 100) if headings else ""


def _context_header(rel: str, raw: str, body: str, offset: int) -> str:
    """Carry stable page identity into both retrievers without storing boilerplate in previews."""
    fields = [
        ("Page", _page_title(rel, raw)),
        ("Area", _page_class(rel, raw)),
        ("Section", _nearest_heading(body, offset)),
        ("Summary", _frontmatter_summary(raw)),
    ]
    header = " | ".join(f"{label}: {value}" for label, value in fields if value)
    return header[:CONTEXT_HEADER_MAX_CHARS].rstrip()


def _prepared_chunks(rel: str, raw: str, contextual: bool) -> list[tuple[str, str]]:
    body = clean_text(raw)
    spans = _chunk_spans(body)
    if not contextual:
        return [(piece, piece) for piece, _ in spans]
    return [
        (piece, f"{_context_header(rel, raw, body, offset)}\n\n{piece}")
        for piece, offset in spans
    ]


# ---------------------------------------------------------------- index
def index(vault: Path | None = None, db: Path | None = None,
          verbose: bool = True, limit_pages: int | None = None,
          contextual: bool = False) -> dict:
    """Embed new or changed chunks. Returns a stats dict; never raises."""
    paths = config.paths()
    vault = Path(vault or paths["vault"])
    db = Path(db or paths["vault_vectors"])
    stats = {"pages": 0, "chunks": 0, "embedded": 0, "reused": 0,
             "deleted": 0, "skipped_pages": 0, "ok": False}

    if _sqlite_vec() is None:
        print("[vault_vectors] sqlite-vec not installed — nothing to do.", file=sys.stderr)
        return stats
    if not config.get("embed_endpoint"):
        print("[vault_vectors] no embed_endpoint configured — nothing to do.", file=sys.stderr)
        return stats
    if not vault.is_dir():
        print(f"[vault_vectors] vault not found: {vault}", file=sys.stderr)
        return stats

    con = _connect(db, create=True)
    if con is None:
        return stats
    dims = vault_embed.dimensions()
    stored = _stored_dimensions(con)
    if stored is not None and stored != dims:
        print(f"[vault_vectors] sidecar was built at {stored} dimensions but "
              f"config asks for {dims}. Delete {db} and re-index, or restore "
              f"embed_dimensions={stored}. Refusing to mix.", file=sys.stderr)
        con.close()
        return stats
    _init(con, dims)

    existing = {(r["path"], r["chunk_ix"]): (r["rowid"], r["content_hash"])
                for r in con.execute("SELECT rowid, path, chunk_ix, content_hash FROM chunks")}
    seen: set[tuple[str, int]] = set()
    pending: list[tuple[str, int, str, str, str]] = []
    # path, ix, hash, indexed text, clean preview
    all_chunks: list[tuple[str, int, str]] = []     # every chunk, for the BM25 rebuild

    page_list = list(_pages(vault))
    if limit_pages is not None:
        page_list = page_list[:limit_pages]
    scope_rels = {p.relative_to(vault).as_posix() for p in page_list}

    for p in page_list:
        rel = p.relative_to(vault).as_posix()
        raw = _read(p)
        pieces = _prepared_chunks(rel, raw, contextual)
        if not pieces:
            stats["skipped_pages"] += 1
            continue
        stats["pages"] += 1
        for ix, (preview, indexed_text) in enumerate(pieces):
            key = (rel, ix)
            seen.add(key)
            stats["chunks"] += 1
            all_chunks.append((rel, ix, indexed_text))
            h = _hash(indexed_text)
            prior = existing.get(key)
            if prior and prior[1] == h:
                stats["reused"] += 1      # unchanged text: no API call
                continue
            pending.append((rel, ix, h, indexed_text, preview))

    # A partial run owns only its selected paths; treating unseen out-of-scope
    # rows as stale was the destructive failure mode that erased the live index.
    stale = [
        v[0] for k, v in existing.items()
        if k not in seen and (limit_pages is None or k[0] in scope_rels)
    ]
    for rid in stale:
        con.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rid,))
        con.execute("DELETE FROM chunks WHERE rowid = ?", (rid,))
    stats["deleted"] = len(stale)

    # Full runs rebuild BM25 wholesale. Partial runs replace only their paths,
    # keeping both out-of-scope rows and their lexical index intact.
    if limit_pages is None:
        con.execute("DELETE FROM chunk_fts")
        con.executemany(
            "INSERT INTO chunk_fts (body, path, chunk_ix) VALUES (?,?,?)",
            [(piece, rel, ix) for rel, ix, piece in all_chunks])
    elif scope_rels:
        placeholders = ",".join("?" for _ in scope_rels)
        con.execute(f"DELETE FROM chunk_fts WHERE path IN ({placeholders})",
                    tuple(sorted(scope_rels)))
        con.executemany(
            "INSERT INTO chunk_fts (body, path, chunk_ix) VALUES (?,?,?)",
            [(piece, rel, ix) for rel, ix, piece in all_chunks])
    con.commit()

    if verbose:
        print(f"[vault_vectors] {stats['pages']} pages, {stats['chunks']} chunks; "
              f"{len(pending)} to embed, {stats['reused']} unchanged, "
              f"{stats['deleted']} removed")

    BATCH = 64
    for i in range(0, len(pending), BATCH):
        group = pending[i:i + BATCH]
        vectors = vault_embed.embed([g[3] for g in group])
        if len(vectors) != len(group):
            print(f"[vault_vectors] embedding failed at batch {i // BATCH}; "
                  f"keeping {stats['embedded']} already written.", file=sys.stderr)
            break
        for (rel, ix, h, _indexed_text, preview), vec in zip(group, vectors):
            prior = existing.get((rel, ix))
            if prior:
                rid = prior[0]
                con.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rid,))
                con.execute("UPDATE chunks SET content_hash = ?, preview = ? "
                            "WHERE rowid = ?", (h, preview[:200], rid))
            else:
                cur = con.execute(
                    "INSERT INTO chunks (path, chunk_ix, content_hash, preview) "
                    "VALUES (?,?,?,?)", (rel, ix, h, preview[:200]))
                rid = cur.lastrowid
            con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                        (rid, _pack(vec)))
            stats["embedded"] += 1
        con.commit()
        if verbose:
            print(f"[vault_vectors]   embedded {stats['embedded']}/{len(pending)}")

    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dimensions', ?)",
                (str(dims),))
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
                (config.get("embed_deployment"),))
    con.commit()
    con.close()
    stats["ok"] = True
    return stats


def _pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# --------------------------------------------------------------- search
RRF_K = 60          # standard Reciprocal Rank Fusion constant
# A relaxed OR match is recall evidence, not equivalent to a strict conjunction.
# Flattening that ladder keeps it useful for discovery without letting a weak
# lexical hit displace a confident semantic result.
RELAXED_RRF_K = 120
STRICT_MIN_RESULTS = 1

# Question words, auxiliaries, pronouns, articles, and connective/prepositional
# words add little topical signal in a tiny FTS chunk. Retaining them in an OR
# fallback lets "how" or "does" outrank the terms that name the subject.
NATURAL_LANGUAGE_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "should", "that", "the", "to",
    "was", "we", "what", "when", "which", "who", "why", "with", "would",
    "you", "your",
})


def _sanitise_fts(q: str) -> str:
    """Quote tokens FTS5 would parse as syntax.

    Same discipline as wiki/vault_mcp.py: paths, emails and filenames are
    exactly what people search for, and every one contains punctuation FTS5
    treats as an operator.
    """
    parts = []
    for tok in q.split():
        if tok.upper() in ("AND", "OR", "NOT") or re.fullmatch(r"\w+\*?", tok):
            parts.append(tok)
        else:
            parts.append('"' + tok.replace('"', '""') + '"')
    return " ".join(parts)


def _has_explicit_fts_syntax(query: str) -> bool:
    """Keep intentional FTS expressions exact rather than weakening their meaning."""
    return (bool(re.search(r'"|\b(?:AND|OR|NOT|NEAR)\b|\w\*', query, re.I))
            or "(" in query or ")" in query)


def _relaxed_fts(query: str) -> str:
    """Build a recall-oriented OR expression from a natural-language question.

    If every token is conversational scaffolding, retain the original
    conjunction instead of turning a vague question into a broad OR search.
    """
    content = []
    for token in query.split():
        word = re.sub(r"^\W+|\W+$", "", token).casefold()
        if word and word not in NATURAL_LANGUAGE_STOPWORDS:
            content.append(_sanitise_fts(token))
    return " OR ".join(content) if content else _sanitise_fts(query)


def _is_natural_language_query(query: str) -> bool:
    """Do not weaken a zero-hit keyword lookup just because it has many terms."""
    return any(
        re.sub(r"^\W+|\W+$", "", token).casefold() in NATURAL_LANGUAGE_STOPWORDS
        for token in query.split()
    )


def _bm25_rows(con: sqlite3.Connection, sql: str, query: str, k: int):
    """Run the verbatim query first so paths and deliberate FTS syntax survive."""
    try:
        return con.execute(sql, (query, k)).fetchall()
    except sqlite3.OperationalError:
        return con.execute(sql, (_sanitise_fts(query), k)).fetchall()


def search_bm25(query: str, limit: int = 10, db: Path | None = None) -> list[dict]:
    """Real BM25 over chunk text, collapsed to parent pages.

    NOTE the sign convention: SQLite's bm25() is NEGATIVE and more negative is
    better, so ordering is ASCENDING and the best chunk for a page is the
    minimum. Getting this backwards silently inverts relevance.
    """
    if not query.strip():
        return []
    db = Path(db or config.paths()["vault_vectors"])
    con = _connect(db)
    if con is None:
        return []
    sql = """
        SELECT path, chunk_ix, bm25(chunk_fts) AS score
        FROM chunk_fts WHERE chunk_fts MATCH ?
        ORDER BY score LIMIT ?
    """
    k = max(limit * 5, 30)
    branch = "strict"
    try:
        rows = _bm25_rows(con, sql, query, k)
        # FTS5 joins bare tokens with implicit AND. That is desirable for a
        # precise lookup, but makes ordinary questions empty on 1,200-character
        # chunks. Only relax an insufficient natural-language result; explicit
        # FTS syntax is a request for exact semantics, not a spelling mistake.
        if (len(rows) < STRICT_MIN_RESULTS
                and _is_natural_language_query(query)
                and not _has_explicit_fts_syntax(query)):
            relaxed = _relaxed_fts(query)
            if relaxed != query:
                rows = _bm25_rows(con, sql, relaxed, k)
                branch = "relaxed"
    except sqlite3.Error as e:
        print(f"[vault_vectors] bm25 search failed: {e}", file=sys.stderr)
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass

    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["path"])
        if cur is None or r["score"] < cur["score"]:
            best[r["path"]] = {"path": r["path"], "score": r["score"],
                               "chunk_ix": r["chunk_ix"], "branch": branch}
    out = sorted(best.values(), key=lambda d: d["score"])
    for rank, d in enumerate(out, 1):
        d["rank"] = rank
    return out[:limit]


def search_hybrid(query: str, limit: int = 10, db: Path | None = None) -> list[dict]:
    """Reciprocal Rank Fusion of BM25 and vector results.

        score(d) = sum over retrievers of 1 / (RRF_K + rank_i(d))

    RRF fuses *ranks*, not scores, which is why it works here at all: a bm25
    value and a cosine distance are not on comparable scales and normalising
    them against each other would be arbitrary. This is the pattern Azure AI
    Search documents as its default relevance story.

    Degrades cleanly: if either retriever returns nothing -- no sqlite-vec, no
    endpoint, lapsed az login -- this is just the other retriever's ordering.
    """
    if not query.strip():
        return []
    pool = max(limit * 3, 20)
    lex = search_bm25(query, pool, db)
    vec = search(query, pool, db)
    if not lex and not vec:
        return []

    fused: dict[str, dict] = {}
    for source, results in (("bm25", lex), ("vector", vec)):
        for d in results:
            e = fused.setdefault(d["path"], {"path": d["path"], "rrf": 0.0,
                                             "sources": [], "chunk_ix": d.get("chunk_ix")})
            effective_k = (
                RELAXED_RRF_K
                if source == "bm25" and d.get("branch") == "relaxed"
                else RRF_K
            )
            e["rrf"] += 1.0 / (effective_k + d["rank"])
            e["sources"].append(f"{source}#{d['rank']}")
            if source == "vector":
                e["distance"] = d.get("distance")
                e["preview"] = d.get("preview")

    out = sorted(fused.values(), key=lambda d: -d["rrf"])
    for rank, d in enumerate(out, 1):
        d["rank"] = rank
    return out[:limit]


def _embed_query_cached(con: sqlite3.Connection, query: str, dims: int):
    """Embed a query, reusing a cached vector when the same text was seen before.

    Keyed on (dimensions, query) so changing embed_dimensions cannot serve a
    stale vector of the wrong width. Cache failures are always non-fatal: a
    miss, a corrupt row or a read-only DB just costs one API call.
    """
    key = _hash(f"{dims}\x00{query}")
    try:
        row = con.execute("SELECT embedding FROM query_cache WHERE key = ?",
                          (key,)).fetchone()
        if row:
            blob = row[0]
            return list(struct.unpack(f"{len(blob) // 4}f", blob))
    except (sqlite3.Error, struct.error):
        pass
    qv = vault_embed.embed_one(query)
    if not qv:
        return None
    try:
        con.execute("INSERT OR REPLACE INTO query_cache (key, embedding) VALUES (?,?)",
                    (key, _pack(qv)))
        con.commit()
    except sqlite3.Error:
        pass
    return qv


def _stored_dimensions(con: sqlite3.Connection) -> int | None:
    try:
        r = con.execute("SELECT value FROM meta WHERE key = 'dimensions'").fetchone()
        return int(r[0]) if r else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def search(query: str, limit: int = 10, db: Path | None = None) -> list[dict]:
    """Semantic page search. Returns [] whenever vectors are unavailable."""
    if not query.strip():
        return []
    db = Path(db or config.paths()["vault_vectors"])
    con = _connect(db)
    if con is None:
        return []
    try:
        # A sidecar built at one dimension cannot be queried at another: the
        # vec0 MATCH would either error or, worse, compare truncated vectors and
        # return confident nonsense. Refuse rather than guess.
        stored = _stored_dimensions(con)
        want = vault_embed.dimensions()
        if stored is not None and stored != want:
            print(f"[vault_vectors] dimension mismatch: sidecar was built at "
                  f"{stored}, config asks for {want}. Re-run "
                  f"`python wiki\\vault_vectors.py index` after deleting the "
                  f"sidecar, or restore the matching embed_dimensions.",
                  file=sys.stderr)
            return []
        qv = _embed_query_cached(con, query, want)
        if not qv:
            return []
        # Overfetch chunks: several may belong to one page, and the page is the
        # unit we return, so k chunks can collapse to far fewer pages.
        k = max(limit * 5, 30)
        rows = con.execute("""
            SELECT c.path AS path, c.chunk_ix AS chunk_ix, c.preview AS preview,
                   v.distance AS distance
            FROM vec_chunks v JOIN chunks c ON c.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
        """, (_pack(qv), k)).fetchall()
    except sqlite3.Error as e:
        print(f"[vault_vectors] search failed: {e}", file=sys.stderr)
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass

    # Small-to-big: collapse chunk hits to their parent page, keeping the best.
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["path"])
        if cur is None or r["distance"] < cur["distance"]:
            best[r["path"]] = {"path": r["path"], "distance": r["distance"],
                               "chunk_ix": r["chunk_ix"], "preview": r["preview"]}
    out = sorted(best.values(), key=lambda d: d["distance"])
    for rank, d in enumerate(out, 1):
        d["rank"] = rank
    return out[:limit]


def status(db: Path | None = None) -> dict:
    db = Path(db or config.paths()["vault_vectors"])
    st = {"db": str(db), "exists": db.is_file(),
          "sqlite_vec": _sqlite_vec() is not None,
          "endpoint": bool(config.get("embed_endpoint")),
          "chunks": 0, "pages": 0, "dimensions": None, "model": None,
          "size_bytes": db.stat().st_size if db.is_file() else 0}
    con = _connect(db)
    if con is None:
        return st
    try:
        st["chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        st["pages"] = con.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        for r in con.execute("SELECT key, value FROM meta"):
            st[r["key"] if r["key"] != "dimensions" else "dimensions"] = r["value"]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return st


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Vault vector sidecar")
    sub = ap.add_subparsers(dest="cmd")
    pi = sub.add_parser("index", help="embed new or changed chunks")
    pi.add_argument("--limit-pages", type=int, default=None,
                    help="only process the first N pages (smoke testing)")
    pi.add_argument("--db", type=Path, default=None,
                    help="alternate sidecar path (default: configured vault_vectors)")
    pi.add_argument("--contextual", "--contextual-chunks", dest="contextual",
                    action="store_true",
                    help="prepend deterministic page context to indexed chunks")
    pi.add_argument("--quiet", action="store_true")
    pst = sub.add_parser("status", help="show sidecar statistics")
    pst.add_argument("--db", type=Path, default=None)
    ps = sub.add_parser("search", help="semantic search")
    ps.add_argument("query")
    ps.add_argument("--limit", type=int, default=10)
    ps.add_argument("--db", type=Path, default=None)
    a = ap.parse_args()

    if a.cmd == "index":
        s = index(db=a.db, verbose=not a.quiet, limit_pages=a.limit_pages,
                  contextual=a.contextual)
        print(f"\nembedded {s['embedded']}, reused {s['reused']}, "
              f"deleted {s['deleted']} across {s['pages']} pages")
        return 0 if s["ok"] else 1
    if a.cmd == "status":
        for k, v in status(a.db).items():
            print(f"{k:12s}: {v}")
        return 0
    if a.cmd == "search":
        hits = search(a.query, a.limit, a.db)
        if not hits:
            print("no vector results (unavailable, or nothing indexed)")
            return 1
        for h in hits:
            print(f"[{h['distance']:.4f}] {h['path']}")
            if h.get("preview"):
                print(f"          {h['preview'][:110].replace(chr(10), ' ')}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
