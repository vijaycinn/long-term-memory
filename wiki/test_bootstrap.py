"""Prove the wiki bootstraps on a virgin setup.

Runs the whole toolchain against a throwaway COPILOT_HOME and vault, so nothing
touches the real environment. Covers the three modes config.py distinguishes:

  empty        no memory database at all
  memory-only  a database, no source workspace
  full         a database plus a source workspace

The point is that none of these crash, and the middle one still produces a
genuinely useful wiki — that is what "usable on a virgin setup" has to mean.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

WIKI = Path(__file__).resolve().parent
REPO = WIKI.parent
PY = sys.executable
FAILURES = []


def run(args, env, cwd=WIKI, expect_zero=True):
    p = subprocess.run([PY, *args], cwd=str(cwd), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    if expect_zero and p.returncode != 0:
        FAILURES.append(f"{' '.join(args)} -> exit {p.returncode}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
    return p


def seed_memory(db: Path):
    """A minimal but realistic memory.db, including the FTS5 index."""
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE topics (id INTEGER PRIMARY KEY, slug TEXT, title TEXT,
            category TEXT, description TEXT, status TEXT,
            last_accessed_at TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT, fact_type TEXT,
            topic_id INTEGER, confidence INT, importance INT, status TEXT,
            source TEXT, session_id TEXT, last_accessed_at TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT,
            is_self INT, attributes TEXT, notes TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY, topic_id INT, seq_number INT,
            title TEXT, summary TEXT, findings TEXT, decisions TEXT,
            open_questions TEXT, next_steps TEXT, source_session TEXT, created_at TEXT);
        CREATE TABLE ltm_session_log (session_id TEXT PRIMARY KEY, cwd TEXT,
            repository TEXT, summary TEXT, end_reason TEXT, facts_extracted INT,
            entities_extracted INT, patterns_detected INT, processed_at TEXT);
        CREATE TABLE ltm_patterns (id INTEGER PRIMARY KEY, session_id TEXT,
            pattern_type TEXT, description TEXT, repository TEXT, cwd TEXT,
            created_at TEXT);
        CREATE VIRTUAL TABLE memory_fts USING fts5(content);
    """)
    c.execute("INSERT INTO topics VALUES (1,'widget-pricing','Widget Pricing',"
              "'research','How widgets are priced','active','2026-01-02',"
              "'2026-01-01','2026-01-02')")
    rows = [
        (1, "Widget pricing dropped 40% after the analyzer switch.", "finding", 1,
         5, 5, "active", "docs\\pricing.md", "s1", "2026-01-02", "2026-01-02", "2026-01-02"),
        (2, "Never quote list price without checking the discount table.",
         "constraint", 1, 5, 5, "active", None, "s1", "2026-01-02", "2026-01-02", "2026-01-02"),
        (3, "Acme Corp is the largest widget buyer.", "insight", 1, 4, 4,
         "active", None, "s1", "2026-01-02", "2026-01-02", "2026-01-02"),
        # Real facts routinely embed paths, emails and punctuation. FTS5 parses
        # several of these characters as operators, so they are the cases most
        # likely to break search.
        (4, "The pricing table lives at C:\\data\\widgets\\pricing.md and is owned "
            "by ops@example.com.", "finding", 1, 4, 4, "active", None, "s1",
         "2026-01-02", "2026-01-02", "2026-01-02"),
    ]
    c.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for r in rows:
        c.execute("INSERT INTO memory_fts(rowid, content) VALUES (?,?)", (r[0], r[1]))
    c.execute("INSERT INTO entities VALUES (1,'Acme Corp','account',0,'{}',"
              "'Largest widget buyer','2026-01-01','2026-01-02')")
    c.execute("INSERT INTO entities VALUES (2,'Test User','person',1,"
              "'{\"role\":\"analyst\"}','','2026-01-01','2026-01-02')")
    c.execute("INSERT INTO snapshots VALUES (1,1,1,'Pricing review','Prices fell.',"
              "'Analyzer switch was the cause.','Switch all tenants.',"
              "'Does it hold at scale?','Re-measure in Q2','s1','2026-01-02')")
    c.commit()
    c.close()


def env_for(home: Path, vault: Path, workspace: Path | None):
    e = dict(os.environ)
    e["COPILOT_HOME"] = str(home)
    e["LTM_WIKI_VAULT"] = str(vault)
    e["LTM_MEMORY_DB"] = str(home / "memory.db")
    e["LTM_SESSION_DB"] = str(home / "session-store.db")
    e["LTM_WIKI_WORKSPACE"] = str(workspace) if workspace else ""
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{label} {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ltmwiki-"))
    try:
        home, vault = tmp / "home", tmp / "vault"
        home.mkdir()

        # ---------------------------------------------------------- empty
        print("\n=== mode: empty (no memory database) ===")
        e = env_for(home, vault, None)
        p = run(["config.py"], e)
        check("config reports empty mode", "mode          : empty" in p.stdout, p.stdout)
        p = run(["sync.py", "--status"], e)
        check("sync --status survives with no database", p.returncode == 0)
        p = run(["sync.py"], e)
        check("sync exits cleanly with nothing to build", p.returncode == 0, p.stderr[-300:])
        check("sync explains the gap", "no memory database" in p.stdout, p.stdout)

        # ---------------------------------------------------- memory-only
        print("\n=== mode: memory-only (database, no workspace) ===")
        seed_memory(home / "memory.db")
        p = run(["config.py"], e)
        check("config reports memory-only", "mode          : memory-only" in p.stdout, p.stdout)

        p = run(["build.py"], e)
        check("build succeeds with no workspace", p.returncode == 0, p.stderr[-500:])
        check("index.md written", (vault / "index.md").is_file())
        check("log.md written", (vault / "log.md").is_file())
        check("topic page generated",
              any((vault / "05-Knowledge" / "Topics").glob("*.md"))
              if (vault / "05-Knowledge" / "Topics").is_dir() else False)
        check("entity page generated",
              any((vault / "02-People").rglob("Acme Corp.md")))
        check("build state kept with the vault, not the repo",
              (vault / ".wiki" / "state.json").is_file()
              and not (WIKI / "state.json").exists())

        p = run(["validate.py"], e)
        check("validate finds no broken links", "broken links   : 0" in p.stdout, p.stdout[:400])
        p = run(["check_frontmatter.py"], e)
        check("all frontmatter parses", "invalid                   : 0" in p.stdout, p.stdout[:400])
        p = run(["lint.py"], e)
        check("lint runs", p.returncode == 0, p.stderr[-300:])

        # intrinsic FTS — the path that needs no vault and no Obsidian
        req = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "memory_search", "arguments": {"query": "widget pricing"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "memory_search",
                        "arguments": {"query": "C:\\data\\widgets\\pricing.md"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "vault_search", "arguments": {"query": "widget"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "memory_search",
                        "arguments": {"query": "ops@example.com"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "memory_search",
                        "arguments": {"query": 'pricing("table") ^bad:'}}},
        ]
        payload = "\n".join(json.dumps(r) for r in req) + "\n"
        p = subprocess.run([PY, str(WIKI / "vault_mcp.py")], input=payload, env=e,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        msgs = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        by_id = {m.get("id"): m for m in msgs}
        tools = [t["name"] for t in by_id.get(2, {}).get("result", {}).get("tools", [])]
        check("MCP exposes memory_search", "memory_search" in tools, str(tools))
        fts = by_id.get(3, {}).get("result", {}).get("content", [{}])[0].get("text", "")
        check("FTS finds a seeded fact", "40%" in fts, fts[:200])
        punct = by_id.get(4, {}).get("result", {}).get("content", [{}])[0].get("text", "")
        check("FTS finds a Windows path inside a fact", "pricing table" in punct, punct[:200])
        vs = by_id.get(5, {}).get("result", {}).get("content", [{}])[0].get("text", "")
        check("vault_search works against the fresh vault", "match" in vs.lower(), vs[:200])
        mail = by_id.get(6, {}).get("result", {}).get("content", [{}])[0].get("text", "")
        check("FTS finds an email address", "pricing table" in mail, mail[:200])
        # A query containing FTS5 operators must be sanitised, not crash.
        syn = by_id.get(7, {}).get("result", {}).get("content", [{}])[0].get("text", "")
        check("FTS survives operator characters without erroring",
              "failed" not in syn.lower() and "syntax error" not in syn.lower(), syn[:200])

        # answers file back and survive a rebuild
        ans = tmp / "answer.md"
        ans.write_text("Widget prices fell because the analyzer changed.\n", encoding="utf-8")
        p = run(["wiki.py", "file-answer", "Why widget prices fell",
                 "--body-file", str(ans), "--tags", "pricing"], e)
        filed = vault / "10-Synthesis" / "Why widget prices fell.md"
        check("answer filed to 10-Synthesis", filed.is_file(), p.stdout + p.stderr)
        run(["build.py", "--memory-only"], e)
        check("filed answer survives a rebuild", filed.is_file())
        idx = (vault / "index.md").read_text(encoding="utf-8")
        check("filed answer appears in index.md", "Why widget prices fell" in idx)

        # ------------------------------------------------------------ full
        print("\n=== mode: full (database + source workspace) ===")
        ws = tmp / "workspace"
        (ws / "research").mkdir(parents=True)
        (ws / "research" / "widget-study.md").write_text(
            "# Widget study\n\nAcme Corp buys the most widgets.\n", encoding="utf-8")
        e2 = env_for(home, vault, ws)
        p = run(["config.py"], e2)
        check("config reports full mode", "mode          : full" in p.stdout, p.stdout)
        p = run(["build.py"], e2)
        check("full build succeeds", p.returncode == 0, p.stderr[-500:])
        check("workspace document imported",
              any(vault.rglob("Widget study.md")))
        p = run(["validate.py"], e2)
        check("validate still clean after full build",
              "broken links   : 0" in p.stdout, p.stdout[:400])
        check("filed answer still alive after full rebuild", filed.is_file())

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(" -", f[:500])
        return 1
    print("virgin-setup bootstrap verified: empty, memory-only and full all work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
