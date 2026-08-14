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
from datetime import datetime, timezone
from pathlib import Path

BUILD = Path(__file__).resolve().parent
sys.path.insert(0, str(BUILD))
import config

VAULT = config.paths()["vault"]
SUMMARY_RE = re.compile(r'''^summary:\s*(?:"(.*)"|'(.*)')\s*$''', re.M)
PROTOCOL = "2024-11-05"


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
def t_search(args):
    q = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    if not q:
        return "error: query is required"
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


def _resolve(name: str) -> Path | None:
    name = name.strip().strip("[]")
    direct = VAULT / name
    if direct.is_file():
        return direct
    if not name.endswith(".md"):
        direct = VAULT / f"{name}.md"
        if direct.is_file():
            return direct
    target = name[:-3] if name.endswith(".md") else name
    for p in pages():
        if p.stem.lower() == target.lower():
            return p
    return None


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
    return f"# {p.relative_to(VAULT).as_posix()}\n\n{body}"


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
    sql = """
        SELECT f.content   AS content,
               f.fact_type AS kind,
               f.importance AS importance,
               f.source    AS source,
               t.title     AS topic
        FROM memory_fts m
        JOIN facts f ON f.id = m.row_id AND m.table_name = 'facts'
        LEFT JOIN topics t ON t.id = f.topic_id
        WHERE memory_fts MATCH ?
          AND COALESCE(f.status, 'active') = 'active'
        ORDER BY f.importance DESC, f.created_at DESC
        LIMIT ?
    """
    try:
        return con.execute(sql, (q, limit)).fetchall()
    except sqlite3.OperationalError:
        # Verbatim query was not valid FTS5 syntax — retry sanitised.
        return con.execute(sql, (_sanitise_fts(q), limit)).fetchall()


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
            "database directly. Importance 5 facts are load-bearing constraints."),
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
            "Search the LTM knowledge vault — 475 cross-referenced pages "
            "compiled from the agency workspace, long-term memory and 627 past "
            "sessions. USE THIS BEFORE answering from your own recall on: any "
            "customer or account, territory, quota, pipeline, prior research, "
            "past decisions, what happened in an earlier session, GCC rules, MSX "
            "metric definitions, or how a system in this workspace works. "
            "Returns ranked pages with a one-line summary each; follow up with "
            "vault_read."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search terms, e.g. 'content understanding cost'"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_read",
        "description": ("Read a vault page in full by name (e.g. 'SAIF', "
                        "'Topic - saif-cu-cost') or vault-relative path. Cite the "
                        "`source:` field from its frontmatter, not the vault path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page name or path"},
                "max_chars": {"type": "integer", "description": "Truncate (default 24000)"},
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
