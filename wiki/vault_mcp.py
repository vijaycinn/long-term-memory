#!/usr/bin/env python3
"""vault-mcp — expose the LTM knowledge wiki as MCP tools.

Instructions and skills are both loaded at session start and, in the skill's
case, surfaced only when the harness's relevance filter picks them. An MCP tool
is unconditional: it sits in the tool list for every turn, so grounding an
answer in the vault costs one tool call and needs no discipline.

Tools:
  vault_search  ranked lookup over page text, returns path + summary
  vault_read    read a page by name or path
  vault_index   the catalogue, whole or one section
  vault_recent  recent entries from the append-only log
  vault_file    file an answer back as a durable page

Speaks MCP over stdio with newline-delimited JSON-RPC 2.0. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BUILD = Path(__file__).resolve().parent
sys.path.insert(0, str(BUILD))
import config
CORE = BUILD.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
from memory_driver import (
    FTS_OVERFETCH,
    IMPORTANCE_WEIGHT,
    MIN_FTS_POOL,
    RANK_WEIGHT,
    blend_fts_hits,
)

VAULT = config.paths()["vault"]
SUMMARY_RE = re.compile(r'''^summary:\s*(?:"(.*)"|'(.*)')\s*$''', re.M)
PROTOCOL = "2024-11-05"

# ── memory_search ranking ──────────────────────────────────────────────────
RELATED_FACT_CAP = 5       # T1.3: entity-co-mention facts appended per search


def read(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


# The MCP server is a long-lived process, so page text is cached across calls
# and revalidated by mtime. A cold vault_search reads ~480 files; without this
# every repeat search pays that again, which is the difference between a tool an
# agent calls freely and one it avoids.
_CACHE: dict[Path, tuple[float, str, str]] = {}


def read_cached(p: Path) -> tuple[str, str]:
    """Returns (text, summary), re-reading only when the file has changed."""
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return "", ""
    hit = _CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    text = read(p)
    entry = (mtime, text, summary_of(text))
    _CACHE[p] = entry
    return entry[1], entry[2]


def summary_of(text: str) -> str:
    m = SUMMARY_RE.search(text[:1500])
    if not m:
        return ""
    s = m.group(1) if m.group(1) is not None else (m.group(2) or "")
    return s.replace("''", "'")


def pages():
    for p in VAULT.rglob("*.md"):
        if ".obsidian" not in p.parts:
            yield p


# ----------------------------------------------------------------- tools
def _t_search_lexical_v0(q: str, limit: int) -> str:
    """The original substring-AND scorer. Frozen: `mode='lexical'` must keep
    returning byte-identical output forever, because it is the fallback when
    sqlite-vec is missing, the endpoint is unset or `az login` has lapsed."""
    terms = [t.lower() for t in q.split() if t]
    hits = []
    for p in pages():
        text, summary = read_cached(p)
        low = text.lower()
        if not all(t in low for t in terms):
            continue
        score = sum(low.count(t) for t in terms)
        stem = p.stem.lower()
        if all(t in stem for t in terms):
            score += 25          # a title match beats incidental mentions
        hits.append((score, p, summary))
    if not hits:
        return f"no matches for {q!r}"
    hits.sort(key=lambda x: -x[0])
    out = [f"{len(hits)} match(es) for {q!r}; showing {min(limit, len(hits))}", ""]
    for score, p, s in hits[:limit]:
        out.append(f"[{score:>4}] {p.stem}")
        out.append(f"       {p.relative_to(VAULT).as_posix()}")
        if s:
            out.append(f"       {s}")
    return "\n".join(out)


def _render_ranked(rows: list[dict], q: str, label: str) -> str:
    out = [f"{len(rows)} match(es) for {q!r} [{label}]", ""]
    for d in rows:
        rel = d["path"]
        p = VAULT / rel
        summary = read_cached(p)[1] if p.is_file() else ""
        marker = ""
        if "rrf" in d:
            marker = f"[{d['rrf']:.4f}]"
        elif "distance" in d and d["distance"] is not None:
            marker = f"[d={d['distance']:.3f}]"
        elif "score" in d:
            marker = f"[{d['score']:.2f}]"
        out.append(f"{marker:>10} {Path(rel).stem}")
        out.append(f"           {rel}")
        if summary:
            out.append(f"           {summary}")
        if d.get("sources"):
            out.append(f"           via {', '.join(d['sources'])}")
    return "\n".join(out)


def t_search(args):
    q = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    if not q:
        return "error: query is required"
    mode = (args.get("mode") or "hybrid").strip().lower()

    if mode == "lexical":
        return _t_search_lexical_v0(q, limit)

    # Semantic modes are strictly optional. Any failure -- extension missing,
    # nothing indexed, endpoint unset, token lapsed, network down -- falls back
    # to the frozen lexical scorer rather than surfacing an error, so the vault
    # never goes dark because of an optional enhancement.
    try:
        sys.path.insert(0, str(BUILD))
        import vault_vectors
        if mode == "vector":
            rows = vault_vectors.search(q, limit)
        elif mode == "bm25":
            rows = vault_vectors.search_bm25(q, limit)
        elif mode == "hybrid-rrf":
            rows = vault_vectors.search_hybrid(q, limit)
        else:
            mode = "hybrid"
            pool = vault_vectors.search_hybrid(q, max(limit, 20))
            try:
                import vault_rerank
                rows = vault_rerank.rerank(q, pool, limit)
            except Exception:                   # optional precision enhancement
                rows = pool[:limit]
    except Exception as e:                       # noqa: BLE001 - see above
        print(f"[vault_mcp] {mode} search unavailable ({e}); using lexical.",
              file=sys.stderr)
        return _t_search_lexical_v0(q, limit)

    if not rows:
        fallback = _t_search_lexical_v0(q, limit)
        return (f"[{mode} search returned nothing — falling back to lexical. "
                f"If this persists, check `az login` and "
                f"`python wiki\\vault_vectors.py status`.]\n\n{fallback}")
    return _render_ranked(rows, q, mode)


# Stem -> path index for _resolve(). Pages live in subfolders, so the direct
# VAULT/<name>.md probe almost always misses and the old code fell through to a
# full rglob per lookup. That was tolerable for one vault_read; hop traversal
# resolves up to eight targets per call, which turned into eight whole-vault
# scans. The index is rebuilt on a miss, but no more often than every
# REINDEX_AFTER seconds, so a page created mid-session is still found without
# letting a run of unresolvable names rescan the vault once per name.
_STEMS: dict[str, Path] = {}
_STEMS_AT = 0.0
REINDEX_AFTER = 5.0


def _stem_index(force: bool = False) -> dict[str, Path]:
    global _STEMS_AT
    if force or not _STEMS:
        _STEMS.clear()
        for p in pages():
            # setdefault, not assignment: preserves the old "first match in
            # rglob order wins" behaviour when two folders hold the same stem.
            _STEMS.setdefault(p.stem.lower(), p)
        _STEMS_AT = time.time()
    return _STEMS


def _resolve(name: str) -> Path | None:
    name = name.strip().strip("[]")
    direct = VAULT / name
    if direct.is_file():
        return direct
    if not name.endswith(".md"):
        direct = VAULT / f"{name}.md"
        if direct.is_file():
            return direct
    target = (name[:-3] if name.endswith(".md") else name).lower()
    hit = _stem_index().get(target)
    if hit is not None and hit.is_file():
        return hit
    if hit is not None or (time.time() - _STEMS_AT) > REINDEX_AFTER:
        # Either the cached path is gone or the index is stale enough that a
        # miss might be a genuinely new page. Rebuild once and re-check.
        hit = _stem_index(force=True).get(target)
        if hit is not None and hit.is_file():
            return hit
    return None


# ------------------------------------------------------------- link graph
# The vault holds thousands of [[wikilinks]] that no retrieval path has ever
# parsed. Three kinds exist and they are NOT equal signal:
#
#   breadcrumb  the navigation line vaultlib.flush_notes() injects directly
#               under the frontmatter, e.g. "[[Home]] > [[People MOC]]". Pure
#               chrome — every page in a folder carries the identical pair, so
#               it is dropped outright.
#   ## Related  auto-generated by notes.py find_related(): an n-gram name match
#               against the alias table, capped at 45 inbound edges per note.
#               Nobody curated these, so they are weak signal and are labelled
#               as machine-generated when shown.
#   inline      a link written into the body. Strong signal — somebody chose it.
#
# Truncation therefore always drops ## Related links before inline ones.
_LINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
# A breadcrumb is a line made of nothing but wikilinks joined by the U+203A
# separator vaultlib uses. Matching the shape rather than the literal
# "[[Home]]" keeps this working if the MOC hierarchy is renamed.
_BREADCRUMB_RE = re.compile(
    r"^\s*\[\[[^\]\n]+\]\](?:\s*\u203a\s*\[\[[^\]\n]+\]\])*\s*$")
_RELATED_HEADING_RE = re.compile(r"^#{1,6}\s+related\s*$", re.I)
BREADCRUMB_SCAN_LINES = 5      # breadcrumbs only ever sit at the top of a page
NEIGHBOUR_CHAR_CAP = 2000      # ceiling on everything hops=1 appends


def _link_target(raw: str) -> str:
    """[[Target|alias]] and [[Target#heading]] both point at Target."""
    return raw.split("|")[0].split("#")[0].strip()


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _page_links(text: str) -> tuple[list[str], list[str]]:
    """Split a page's outbound links into (inline, related), order preserved.

    Breadcrumbs are excluded entirely. Duplicates are collapsed, and a target
    that appears both inline and under ## Related counts as inline — the
    stronger of the two.
    """
    body = _strip_frontmatter(text)
    inline: list[str] = []
    related: list[str] = []
    in_related = False
    seen_lines = 0
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # Any heading closes a ## Related block; only a Related heading opens one.
            in_related = bool(_RELATED_HEADING_RE.match(stripped))
            continue
        if not stripped:
            continue
        seen_lines += 1
        if seen_lines <= BREADCRUMB_SCAN_LINES and _BREADCRUMB_RE.match(stripped):
            continue
        bucket = related if in_related else inline
        for raw in _LINK_RE.findall(line):
            t = _link_target(raw)
            if t:
                bucket.append(t)
    seen = set()
    out_inline, out_related = [], []
    for src, dst in ((inline, out_inline), (related, out_related)):
        for t in src:
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            dst.append(t)
    return out_inline, out_related


def _neighbours(text: str, limit: int = 8) -> list[tuple[str, str, str]]:
    """Resolved outbound neighbours as (title, summary, kind) triples.

    The plan asked for (title, summary) pairs; `kind` is carried alongside
    because the caller has to label auto-generated ## Related links differently
    from curated inline ones, and re-deriving that after the fact would mean
    parsing the page twice. Inline neighbours come first, always.

    Summaries come from read_cached(), so a repeat hop over the same neighbour
    costs a stat() rather than a file read.
    """
    inline, related = _page_links(text)
    out: list[tuple[str, str, str]] = []
    seen: set[Path] = set()
    for kind, targets in (("inline", inline), ("related", related)):
        for t in targets:
            if len(out) >= limit:
                return out
            p = _resolve(t)
            if p is None or p in seen:
                continue        # aliases can point at the same page
            seen.add(p)
            _, summary = read_cached(p)
            out.append((p.stem, summary, kind))
    return out


def _linked_block(text: str, limit: int = 8, cap: int = NEIGHBOUR_CHAR_CAP) -> str:
    """Render the `## Linked pages` block, or "" when there is nothing to add.

    Titles and one-line summaries only. Inlining neighbour bodies is what blows
    the context window — a single hub page has 45 inbound edges, and at ~4 KB a
    page that is 180 KB of text for one read.
    """
    ns = _neighbours(text, limit)
    if not ns:
        return ""
    head = ("\n\n## Linked pages\n\n"
            f"Outbound links from this page (titles + summaries only, capped at "
            f"{cap} chars). Call vault_read on any of them to read it in full.\n")
    parts: list[str] = []
    used = 0
    shown = 0
    current_kind = None
    for title, summary, kind in ns:
        chunk = ""
        if kind != current_kind:
            chunk += ("\n### Linked from the body\n" if kind == "inline" else
                      "\n### From the auto-generated `## Related` block "
                      "(name matches, not curated)\n")
        chunk += f"- [[{title}]]" + (f" — {summary}" if summary else "") + "\n"
        if used + len(chunk) > cap:
            break               # inline links come first, so ## Related drops first
        parts.append(chunk)
        used += len(chunk)
        current_kind = kind
        shown += 1
    if not shown:
        return ""
    if shown < len(ns):
        parts.append(f"\n[{len(ns) - shown} more link(s) omitted at the {cap}-char cap]\n")
    return head + "".join(parts)


def t_read(args):
    name = args.get("page") or ""
    p = _resolve(name)
    if not p:
        near = [x.stem for x in pages() if name.lower() in x.stem.lower()][:8]
        hint = ("\nDid you mean: " + ", ".join(near)) if near else ""
        return f"page not found: {name!r}{hint}"
    text = read(p)
    limit = int(args.get("max_chars") or 24000)
    body = text[:limit]
    if len(text) > limit:
        body += f"\n\n[truncated at {limit} of {len(text)} chars]"
    out = f"# {p.relative_to(VAULT).as_posix()}\n\n{body}"
    # hops defaults to 0 so the unset call is byte-identical to the pre-graph
    # behaviour — that is the rollback property, and a test asserts it.
    hops = int(args.get("hops") or 0)
    if hops >= 1:
        out += _linked_block(text)
    return out


def t_index(args):
    idx = VAULT / "index.md"
    if not idx.is_file():
        return "index.md not found — run: python build.py"
    text = read(idx)
    section = (args.get("section") or "").strip()
    if not section:
        head = text.split("\n## ", 1)[0]
        sections = re.findall(r"^## (.+)$", text, re.M)
        return (head + "\n\nSections available (pass `section` to read one):\n"
                + "\n".join(f"- {s}" for s in sections))
    for block in text.split("\n## ")[1:]:
        title = block.split("\n", 1)[0].strip()
        if section.lower() in title.lower():
            return "## " + block[:24000]
    return f"section {section!r} not found"


def t_recent(args):
    log = VAULT / "log.md"
    if not log.is_file():
        return "no log yet"
    n = int(args.get("count") or 15)
    entries = [l for l in read(log).splitlines() if l.startswith("## [")]
    return "\n".join(e[3:] for e in entries[-n:]) or "log is empty"


def t_file(args):
    title = (args.get("title") or "").strip()
    body = args.get("body") or ""
    if not title or not body.strip():
        return "error: title and body are required"
    tmp = BUILD / f"._mcp_answer_{os.getpid()}.md"
    tmp.write_text(body, encoding="utf-8")
    try:
        cmd = [sys.executable, str(BUILD / "wiki.py"), "file-answer", title,
               "--body-file", str(tmp)]
        for flag, key in (("--question", "question"), ("--tags", "tags"),
                          ("--sources", "sources"), ("--summary", "summary")):
            if args.get(key):
                cmd += [flag, str(args[key])]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return (r.stdout or "") + (r.stderr or "") or "filed"
    finally:
        tmp.unlink(missing_ok=True)


def t_memory_search(args):
    """FTS5 search straight against memory.db.

    This is the path that works before any vault exists — on a new machine, or
    for anyone who never opens Obsidian. memory.db ships an FTS5 index, so this
    needs no build step and no external search engine.
    """
    q = (args.get("query") or "").strip()
    if not q:
        return "error: query is required"
    limit = int(args.get("limit") or 15)
    db = config.paths()["memory_db"]
    if not db.is_file():
        return f"no memory database at {db}"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = _fts_query(con, q, limit)
        # Graph expansion runs on the same connection while it is still open.
        related = _related_facts(con, [r["fact_id"] for r in rows])
    except sqlite3.Error as e:
        return f"search failed: {e}"
    finally:
        try:
            con.close()
        except Exception:
            pass
    if not rows:
        return f"no matches for {q!r}"
    out = [f"{len(rows)} match(es) for {q!r}", ""]
    for r in rows:
        kind = r["kind"] or "fact"
        imp = f" imp{r['importance']}" if r["importance"] else ""
        topic = f" [{r['topic']}]" if r["topic"] else ""
        out.append(f"- ({kind}{imp}){topic} {r['content']}")
        if r["source"]:
            out.append(f"    source: {r['source']}")
    if related:
        # A distinct "+" bullet, not "-": callers (and wiki/test_vault_mcp.py)
        # parse direct hits off "\n- " and every direct hit must contain the
        # search term. A co-mentioned fact usually does not, so it must not be
        # mistakable for a direct hit — by marker or by position.
        out.append("")
        out.append(f"## related_facts ({len(related)}) — co-mentioned via a shared "
                   "entity, NOT matches for the query")
        for r in related:
            kind = r["kind"] or "fact"
            imp = f" imp{r['importance']}" if r["importance"] else ""
            topic = f" [{r['topic']}]" if r["topic"] else ""
            out.append(f"+ ({kind}{imp}){topic} {r['content']}")
            if r["via"]:
                out.append(f"    via entity: {r['via']}")
    return "\n".join(out)


def _sanitise_fts(q: str) -> str:
    """Quote bare tokens that FTS5 would treat as syntax.

    Paths, emails and filenames are exactly what gets searched here, and every
    one of them contains punctuation FTS5 parses as an operator. Anything that
    is not a plain word (or a trailing-wildcard prefix) is quoted; deliberate
    syntax such as OR / NOT / "phrase" only survives because sanitising runs
    after the verbatim query has failed.
    """
    parts = []
    for tok in q.split():
        if tok.upper() in ("AND", "OR", "NOT") or re.fullmatch(r"\w+\*?", tok):
            parts.append(tok)
        else:
            parts.append('"' + tok.replace('"', '""') + '"')
    return " ".join(parts)


def _fts_query(con, q: str, limit: int):
    # memory_fts is a COMBINED index over facts, entities, topics and snapshots,
    # so its implicit rowid is its own sequence and does NOT equal facts.id.
    # (Observed: 924 fts rows with max rowid 1127 against 669 facts with max id
    # 683.) Joining on m.rowid silently returned a different fact than the one
    # that matched -- every result was wrong, which is worse than no results
    # because it reads as a confident answer. The triggers store the real key in
    # the row_id column, alongside table_name to disambiguate the source table.
    #
    # The score is aliased bm25_rank, not rank: `rank` is a hidden column FTS5
    # exposes on the virtual table itself, so `ORDER BY rank` in a joined query
    # is ambiguous between the alias and that column. They happen to hold the
    # same value here, but relying on that is a trap for the next person to add
    # a weighted bm25() call.
    sql = """
        SELECT m.row_id     AS id,
               m.table_name AS src,
               f.id         AS fact_id,
               COALESCE(
                   f.content,
                   e.name || ' ' || COALESCE(e.notes, ''),
                   tp.slug || ' ' || tp.title || ' ' || COALESCE(tp.description, ''),
                   COALESCE(s.summary, '') || ' ' || COALESCE(s.findings, '')
               )            AS content,
               COALESCE(f.fact_type, e.entity_type, tp.category) AS kind,
               f.importance AS importance,
               f.source     AS source,
               f.created_at AS created_at,
               t.title      AS topic,
               bm25(memory_fts) AS bm25_rank
        FROM memory_fts m
        LEFT JOIN facts f ON f.id = m.row_id AND m.table_name = 'facts'
        LEFT JOIN topics t ON t.id = f.topic_id
        LEFT JOIN entities e ON e.id = m.row_id AND m.table_name = 'entities'
        LEFT JOIN topics tp ON tp.id = m.row_id AND m.table_name = 'topics'
        LEFT JOIN snapshots s ON s.id = m.row_id AND m.table_name = 'snapshots'
        WHERE memory_fts MATCH ?
          AND (m.table_name != 'facts' OR COALESCE(f.status, 'active') = 'active')
        ORDER BY bm25_rank
        LIMIT ?
    """
    # Overfetch before blending: importance can only lift a fact into the top-N
    # if the fact is in the candidate pool at all. Ranking the exact top-`limit`
    # by relevance and then reshuffling those few would let importance reorder
    # but never rescue.
    pool = max(limit * FTS_OVERFETCH, MIN_FTS_POOL)
    try:
        rows = con.execute(sql, (q, pool)).fetchall()
    except sqlite3.OperationalError:
        # Verbatim query was not valid FTS5 syntax — retry sanitised.
        rows = con.execute(sql, (_sanitise_fts(q), pool)).fetchall()
    return _blend(rows, limit)


def _blend(rows, limit: int) -> list[dict]:
    """Combine relevance with fact importance through the shared scorer.

    Entities, topics, and snapshots have no importance field. The shared scorer
    deliberately treats that absence as 0 rather than inventing a priority.
    Its sign warning, degenerate handling, and deterministic tie-break are the
    contract shared with Search-Memory.
    """
    return blend_fts_hits(rows, limit)


def _related_facts(con, hit_ids: list[int], limit: int = RELATED_FACT_CAP) -> list[dict]:
    """Facts that share an entity with a direct hit — the graph half of a search.

    entity_mentions is a polymorphic junction (entity -> fact | topic |
    snapshot). Both indexes this needs already exist: idx_mentions_entity and
    idx_mentions_parent (setup/init-memory.sql:235-236). No migration.

    These are APPENDED to the answer, never merged into the ranked list: a
    co-mention is a much weaker claim than an FTS match, and letting one displace
    a direct hit would trade a precise answer for an associative one. Facts
    already returned as direct hits are excluded so nothing appears twice.

    Degrades to [] on any error or when no edges exist, which is the state of a
    database that has not had ops/backfill_entity_mentions.py run against it.
    """
    if not hit_ids:
        return []
    marks = ",".join("?" * len(hit_ids))
    sql = f"""
        SELECT f.id          AS fact_id,
               f.content     AS content,
               f.fact_type   AS kind,
               f.importance  AS importance,
               f.source      AS source,
               t.title       AS topic,
               GROUP_CONCAT(DISTINCT e.name) AS via
        FROM entity_mentions hit
        JOIN entity_mentions other
          ON other.entity_id = hit.entity_id
         AND other.parent_type = 'fact'
        JOIN entities e ON e.id = hit.entity_id
        JOIN facts f    ON f.id = other.parent_id
        LEFT JOIN topics t ON t.id = f.topic_id
        WHERE hit.parent_type = 'fact'
          AND hit.parent_id IN ({marks})
          AND other.parent_id NOT IN ({marks})
          AND COALESCE(f.status, 'active') = 'active'
        GROUP BY f.id
        ORDER BY f.importance DESC, f.id
        LIMIT ?
    """
    try:
        rows = con.execute(sql, (*hit_ids, *hit_ids, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Full-text search over long-term memory (SQLite FTS5) — every fact "
            "recorded across all past sessions, with its topic, importance and "
            "source. USE THIS FIRST for 'what do we know about X', 'have we hit "
            "this before', 'what did we decide', or before asserting anything "
            "about an account, product behaviour, past work or a working "
            "preference. Works with no vault and no Obsidian — it queries the "
            "database directly. Importance 5 facts are load-bearing constraints. "
            "Results are ranked by blended relevance (bm25) and importance. When "
            "a hit's entities also appear on facts in other topics, those are "
            "appended as a separate `## related_facts` block (max 5, marked with "
            "`+`) — they are context, not direct answers to the query."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search terms; supports OR, NOT, \"phrase\", prefix*"},
                "limit": {"type": "integer", "description": "Max results (default 15)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_search",
        "description": (
            "Search the LTM knowledge vault — a cross-referenced wiki "
            "compiled from the agency workspace, long-term memory and past "
            "sessions. USE THIS BEFORE answering from your own recall on: any "
            "customer or account, territory, quota, pipeline, prior research, "
            "past decisions, what happened in an earlier session, GCC rules, MSX "
            "metric definitions, or how a system in this workspace works. "
            "Defaults to hybrid search (BM25 + embeddings fused by Reciprocal "
            "Rank Fusion, then a local page-level precision reranker), which "
            "finds pages that paraphrase your wording rather than repeating it. "
            "Falls back to lexical automatically if semantic search is unavailable. "
            "Returns ranked pages with a one-line summary each; follow up with "
            "vault_read."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search terms, e.g. 'content understanding cost'"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
                "mode": {"type": "string",
                         "enum": ["hybrid", "hybrid-rrf", "vector", "bm25", "lexical"],
                         "description": (
                             "hybrid (default) fuses BM25 and embeddings, then "
                             "reranks its candidate pool. hybrid-rrf disables "
                             "that optional rerank and returns raw RRF order. "
                             "vector is pure semantic, best for "
                             "paraphrased wording. bm25 is keyword relevance, "
                             "fastest (~16 ms) and needs no network. lexical is "
                             "the original substring match, kept as the "
                             "guaranteed-offline fallback.")},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_read",
        "description": ("Read a vault page in full by name (e.g. 'SAIF', "
                        "'Topic - saif-cu-cost') or vault-relative path. Cite the "
                        "`source:` field from its frontmatter, not the vault path. "
                        "Pass hops=1 to also list the pages this one links to — "
                        "use it when the answer may live one page away."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page name or path"},
                "max_chars": {"type": "integer", "description": "Truncate (default 24000)"},
                "hops": {"type": "integer",
                         "description": (
                             "0 (default) returns the page alone. 1 appends a "
                             "'## Linked pages' block of neighbour titles + "
                             "one-line summaries — never their bodies — capped at "
                             "2000 chars total. Inline body links are listed first "
                             "and are stronger signal; links from the page's "
                             "'## Related' block are auto-generated name matches, "
                             "labelled as such, and dropped first when truncating.")},
            },
            "required": ["page"],
        },
    },
    {
        "name": "vault_index",
        "description": ("The vault catalogue — every page with a one-line gist. "
                        "Call with no argument for the section list and totals, or "
                        "pass a section such as '02-People' or '03-Research'."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                            "description": "Section, e.g. '02-People'. Omit for overview."},
            },
        },
    },
    {
        "name": "vault_recent",
        "description": "Recent entries from the vault's append-only log — what changed and when.",
        "inputSchema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "Default 15"}},
        },
    },
    {
        "name": "vault_file_answer",
        "description": (
            "File an analysis back into the vault as a durable page. Use after "
            "producing a comparison, analysis or connection worth keeping — "
            "otherwise it is lost with the chat. The page is auto-linked to the "
            "accounts and topics it mentions and survives every rebuild."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown body"},
                "question": {"type": "string", "description": "The question it answers"},
                "tags": {"type": "string", "description": "Comma-separated"},
                "sources": {"type": "string", "description": "Comma-separated source paths"},
                "summary": {"type": "string", "description": "One-line gist for the index"},
            },
            "required": ["title", "body"],
        },
    },
]

HANDLERS = {
    "memory_search": t_memory_search,
    "vault_search": t_search,
    "vault_read": t_read,
    "vault_index": t_index,
    "vault_recent": t_recent,
    "vault_file_answer": t_file,
}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            result = {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "vault", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = req.get("params") or {}
            fn = HANDLERS.get(params.get("name"))
            if not fn:
                result = {"content": [{"type": "text",
                                       "text": f"unknown tool: {params.get('name')}"}],
                          "isError": True}
            else:
                try:
                    text = fn(params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": text}]}
                except Exception as e:                       # never kill the server
                    result = {"content": [{"type": "text", "text": f"error: {e}"}],
                              "isError": True}
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue                                          # no response for notifications
        elif method == "ping":
            result = {}
        else:
            if rid is None:
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601,
                                        "message": f"method not found: {method}"}}),
                  flush=True)
            continue

        if rid is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
