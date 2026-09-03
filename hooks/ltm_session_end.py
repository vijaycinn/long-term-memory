#!/usr/bin/env python3
"""LTM Session-End Hook — extracts entities, facts, patterns from Copilot CLI sessions into memory.db.
Input (stdin): JSON {timestamp, cwd, reason}  Output: None (sessionEnd hooks ignore stdout)
"""
import json, os, re, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

# The repository is organised by function - core/ holds the storage engine,
# ops/ the maintenance scripts, hooks/ this file. Nothing is installed as a
# package, so the sibling folders have to go on the path explicitly. Doing it
# once here rather than at each call site means a new cross-folder import does
# not have to remember to repeat it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sib in ("core", "ops", "hooks", "wiki"):
    _p = os.path.join(_REPO, _sib)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from backfill_entity_mentions import compile_entity_matchers, matching_entity_ids

DEFAULT_DB = str(config.paths()["memory_db"])

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON"); conn.execute("PRAGMA journal_mode = WAL")
    return conn

def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ltm_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            pattern_type TEXT NOT NULL, description TEXT, repository TEXT, cwd TEXT,
            created_at TEXT DEFAULT (datetime('now')), UNIQUE(session_id, pattern_type));
        CREATE TABLE IF NOT EXISTS ltm_session_log (
            session_id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, summary TEXT,
            end_reason TEXT, facts_extracted INTEGER DEFAULT 0,
            entities_extracted INTEGER DEFAULT 0, patterns_detected TEXT,
            processed_at TEXT DEFAULT (datetime('now')));
    """)
    conn.commit()

def find_session_store() -> Path | None:
    candidates = [Path.home() / ".copilot" / "session-store.db"]
    ch = os.environ.get("COPILOT_HOME")
    if ch: candidates.append(Path(ch) / "session-store.db")
    return next((p for p in candidates if p.exists()), None)

def get_repo_identifier(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, cwd=cwd, timeout=5)
        if r.returncode == 0:
            url = r.stdout.strip()
            for pfx in ["https://github.com/", "git@github.com:",
                         "https://dev.azure.com/", "git@ssh.dev.azure.com:v3/"]:
                if url.startswith(pfx): url = url[len(pfx):]
            return url.removesuffix(".git").strip("/")
    except Exception: pass
    return None

def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def find_current_session(sc, cwd, repo_id):
    d = _esc(Path(cwd).name)
    if repo_id:
        row = sc.execute("SELECT id,cwd,repository,branch,summary,created_at FROM sessions "
            "WHERE repository=? OR cwd LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 1",
            (repo_id, f"%{d}%")).fetchone()
    elif d:
        row = sc.execute("SELECT id,cwd,repository,branch,summary,created_at FROM sessions "
            "WHERE cwd LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 1", (f"%{d}%",)).fetchone()
    else: return None
    return dict(row) if row else None

def get_session_turns(sc, sid):
    return [dict(r) for r in sc.execute(
        "SELECT turn_index,user_message,assistant_response FROM turns "
        "WHERE session_id=? ORDER BY turn_index", (sid,)).fetchall()]

# ── Extraction ────────────────────────────────────────────────────────
_PERSON_RE = re.compile(
    r'(?:[Mm]eeting with|[Ff]rom|[Cc]all with|[Ss]ync with|[Tt]alked to|'
    r'[Ss]poke with|[Ee]mail from|[Ee]mail to|[Mm]essage from|[Cc]hat with)'
    r'\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})')
_NAMED_RE = re.compile(
    r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s+'
    r'(?:mentioned|said|suggested|asked|confirmed|approved)')
_STOP_WORDS = {"the","this","that","here","also","with","from","have","been","will"}

def extract_entities(turns, mc, sid):
    txt = "\n".join((t.get("user_message") or "")+" "+(t.get("assistant_response") or "") for t in turns)
    accts = {r["name"].lower(): r["id"] for r in mc.execute(
        "SELECT id,name FROM entities WHERE entity_type='account'").fetchall()}
    names = {m.group(1).strip() for m in _PERSON_RE.finditer(txt)}
    names |= {m.group(1).strip() for m in _NAMED_RE.finditer(txt)}
    count = 0
    for name in names:
        if len(name) < 3 or name.lower() in _STOP_WORDS: continue
        mc.execute("INSERT OR IGNORE INTO entities(name,entity_type,notes) VALUES(?,?,?)",
                   (name, "person", f"auto-detected in session {sid}"))
        count += mc.total_changes > 0
    lo = txt.lower()
    for an, aid in accts.items():
        if an in lo:
            tr = mc.execute("SELECT id FROM topics WHERE status='active' AND "
                "(lower(slug) LIKE ? OR lower(title) LIKE ?) LIMIT 1",
                (f"%{an[:12]}%", f"%{an[:12]}%")).fetchone()
            if tr:
                mc.execute("INSERT OR IGNORE INTO entity_mentions"
                    "(entity_id,parent_id,parent_type,relationship) VALUES(?,?,?,?)",
                    (aid, tr["id"], "topic", "mentions"))
    return count

_CORR_RE = re.compile(
    r'(?:^|\.\s+|;\s+)(?:'
    r'no[, ]|actually |instead |don\'t |do not |i prefer |always use |never use |'
    r'stop (?:doing |using |putting )|that\'s (?:wrong|incorrect|not right)|'
    r'wrong[, \u2014\u2013-]|please don\'t |should be |'
    r'not like that|use .+ instead|i don\'t (?:like|want)|'
    r'from now on |going forward )',
    re.IGNORECASE | re.MULTILINE)
_DEC_RE = re.compile(
    r'(?:let\'s go with|we decided|the approach is|use \w+ for |'
    r'going with|decided to|plan is to|we\'ll use|switch to|'
    r'we agreed|we chose|confirmed:|the decision (?:is|was)|'
    r'we\'re going (?:with|to)|approved:|go ahead with|'
    r'finalized:|locked in)', re.IGNORECASE)

def _match_topic(text, conn, cache):
    lo = text.lower()
    if lo in cache: return cache[lo]
    rows = conn.execute("SELECT id,slug,title FROM topics WHERE status='active'").fetchall()
    for r in rows:
        if r["slug"] in lo or r["title"].lower() in lo:
            cache[lo] = r["id"]; return r["id"]
    for r in rows:
        for w in r["slug"].split("-"):
            if len(w) >= 5 and w in lo:
                cache[lo] = r["id"]; return r["id"]
    cache[lo] = None; return None

def extract_facts(turns, mc, sid):
    count, tc = 0, {}
    for t in turns:
        msg = t.get("user_message") or ""
        if not msg or len(msg) > 2000: continue
        ft = "preference" if _CORR_RE.search(msg) else ("decision" if _DEC_RE.search(msg) else None)
        if not ft: continue
        content = msg[:300].strip()
        tid = _match_topic(content, mc, tc)
        if mc.execute("SELECT id FROM facts WHERE topic_id IS ? AND content=?", (tid, content)).fetchone():
            continue
        mc.execute("INSERT INTO facts(topic_id,content,fact_type,confidence,importance,source,"
                   "session_id,last_accessed_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                   (tid, content, ft, 3, 4, "auto-extracted from session", sid))
        count += 1
    return count


def record_session_fact_entity_mentions(mc, sid):
    """Link only this session's active facts, keeping the hook's work bounded."""
    facts = mc.execute(
        "SELECT id, content FROM facts WHERE session_id=? AND status='active'", (sid,)
    ).fetchall()
    entities = mc.execute("SELECT id, name FROM entities").fetchall()
    matchers = compile_entity_matchers(entities)
    for fact in facts:
        for entity_id in matching_entity_ids(fact["content"], matchers):
            mc.execute(
                """INSERT OR IGNORE INTO entity_mentions
                   (entity_id, parent_id, parent_type, relationship)
                   VALUES (?, ?, 'fact', 'mentions')""",
                (entity_id, fact["id"]),
            )

_PEND = ["todo","next step","remaining","still need","pending","incomplete","not yet","will need to"]

def extract_pending_work(turns, reason, mc, sid):
    if reason not in ("user_exit", "abort", "timeout"): return 0
    resp = next(((t.get("assistant_response") or "")[:1000] for t in reversed(turns)
                 if t.get("assistant_response")), None)
    if not resp: return 0
    lo = resp.lower()
    for ph in _PEND:
        if ph in lo:
            i = lo.index(ph); snip = resp[max(0,i-20):min(len(resp),i+120)].strip()
            content = f"Pending: ...{snip}..."
            if mc.execute("SELECT id FROM facts WHERE content=? AND session_id=?", (content, sid)).fetchone():
                continue
            mc.execute("INSERT INTO facts(topic_id,content,fact_type,confidence,importance,source,"
                       "session_id,last_accessed_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                       (None, content, "todo", 2, 4,
                       "auto-extracted from session (pending work)", sid))
            return 1
    return 0

_PAT_KW = {
    "customer-engagement": ["meeting","call","customer","review","sync"],
    "investigation": ["investigate","analyze","explore","research","understand"],
    "email-drafting": ["email","draft","outlook","send","reply"],
    "documentation": ["document","report","wiki","html","analysis"],
    "msx-activity": ["msx","milestone","opportunity","pipeline","hok"],
    "debugging": ["debug","error","fix","bug","429","throttle"],
    "architecture": ["architecture","design","ads","pattern","solution"],
}

def classify_session(turns, session, mc, repo_id):
    txt = " ".join(((t.get("user_message") or "")+" "+(t.get("assistant_response") or "")).lower() for t in turns)
    if session.get("summary"): txt += " " + session["summary"].lower()
    hits = {p: sum(txt.count(k) for k in kws) for p, kws in _PAT_KW.items()}
    classified = [p for p, c in sorted(hits.items(), key=lambda x: -x[1]) if c >= 2][:3]
    desc = (session.get("summary") or "session")[:100]
    for p in classified:
        mc.execute("INSERT OR IGNORE INTO ltm_patterns(session_id,pattern_type,description,"
            "repository,cwd) VALUES(?,?,?,?,?)", (session["id"], p, desc, repo_id, session.get("cwd")))
    return classified

def touch_relevant_topics(turns, mc):
    txt = " ".join(((t.get("user_message") or "")+" "+(t.get("assistant_response") or "")).lower() for t in turns)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in mc.execute("SELECT id,slug,title FROM topics WHERE status='active'").fetchall():
        if r["slug"] in txt or r["title"].lower() in txt:
            mc.execute("UPDATE topics SET last_accessed_at=? WHERE id=?", (now, r["id"]))

# ── Main ──────────────────────────────────────────────────────────────
def process_session(hook_input, *, memory_db=None, store_path=None):
    cwd = hook_input.get("cwd", os.getcwd())
    end_reason = hook_input.get("reason", "complete")
    if store_path is None: store_path = find_session_store()
    if not store_path or not store_path.exists(): return None
    db_path = str(config.paths(memory_db=memory_db)["memory_db"])
    repo_id = get_repo_identifier(cwd) if os.path.isdir(cwd) else None
    sc = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True, timeout=5)
    sc.row_factory = sqlite3.Row
    session = find_current_session(sc, cwd, repo_id)
    if not session: sc.close(); return None
    sid = session["id"]; turns = get_session_turns(sc, sid); sc.close()
    if not turns: return None
    mc = get_conn(db_path); _ensure_tables(mc)
    if mc.execute("SELECT session_id FROM ltm_session_log WHERE session_id=?", (sid,)).fetchone():
        mc.close(); return None
    ent_n = extract_entities(turns, mc, sid)
    fact_n = extract_facts(turns, mc, sid) + extract_pending_work(turns, end_reason, mc, sid)
    record_session_fact_entity_mentions(mc, sid)
    pats = classify_session(turns, session, mc, repo_id)
    touch_relevant_topics(turns, mc)
    mc.execute("INSERT OR IGNORE INTO ltm_session_log(session_id,cwd,repository,summary,"
        "end_reason,facts_extracted,entities_extracted,patterns_detected) VALUES(?,?,?,?,?,?,?,?)",
        (sid, cwd, repo_id, session.get("summary"), end_reason, fact_n, ent_n, json.dumps(pats)))
    mc.commit(); mc.close()
    return {"session_id": sid, "facts": fact_n, "entities": ent_n, "patterns": pats}

def sync_agent_memory(hook_input, since_hours=48):
    """Replay harness `store_memory` calls into the local store.

    The agent-facing `store_memory` tool writes to the harness's own memory
    service, which `export_context` never reads, so those facts are invisible at
    the next session start. Running this before refresh_ltm_instructions means
    anything the agent saved this session lands in the regenerated bridge.

    Bounded by since_hours because the event logs are tens of MB each and a full
    scan of every session would make the hook slow. Best-effort: a sync problem
    must never break session end.
    """
    try:
        import memory_driver
        return memory_driver.sync_agent_memory({
            "db_path": hook_input.get("memory_db") or DEFAULT_DB,
            "since_hours": since_hours,
        })
    except Exception:
        return None


def refresh_ltm_instructions(hook_input):
    """Re-generate the LTM block in copilot-instructions.md so the NEXT session loads it."""
    try:
        from ltm_session_start import generate_instructions
        generate_instructions(hook_input)
    except Exception:
        pass  # best-effort; sessionStart hook is the backup


def auto_backup(hook_input, interval_hours=12):
    """Take a verified snapshot if the newest one is older than interval_hours.

    This is what makes continuity automatic: without it, backups depend on
    somebody remembering to run Backup-Memory. Throttled so it costs nothing on
    back-to-back sessions, and best-effort so a backup problem can never break
    the hook.
    """
    try:
        import ltm_backup

        payload = {}
        if hook_input.get("db_path"):
            payload["db_path"] = hook_input["db_path"]
        if hook_input.get("backup_dir"):
            payload["backup_dir"] = hook_input["backup_dir"]

        inv = ltm_backup.list_backups(payload)
        newest = inv["backups"][0] if inv["backups"] else None
        if newest and newest["age_hours"] < interval_hours:
            return {"skipped": True, "age_hours": newest["age_hours"]}

        return ltm_backup.backup(dict(payload, keep=hook_input.get("keep", 14)))
    except Exception:
        return None  # best-effort; never break the hook


def sync_obsidian_vault(hook_input, mode="memory-only"):
    """Bring the knowledge wiki level with memory at session end.

    Session end is when the session's facts, entities and checkpoints have all
    landed, so it is the point at which a rebuild captures everything. Runs the
    incremental path rather than a full workspace rescan; a full rebuild is a
    manual `Sync-Vault -Full`.

    Delegates to wiki/sync.py, which owns path resolution, the dirty marker and
    the lock. Best-effort — a wiki problem must never break the hook or lose
    memory data, and when the wiki layer is absent this is a no-op.
    """
    try:
        wiki_dir = (hook_input.get("wiki_dir")
                    or os.environ.get("LTM_WIKI_DIR")
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki"))
        sync_py = os.path.join(wiki_dir, "sync.py")
        if not os.path.isfile(sync_py):
            return None
        if hook_input.get("skip_vault_sync"):
            return {"skipped": True, "reason": "disabled"}
        args = [sys.executable, sync_py, "--quiet"]
        if mode != "memory-only":
            args.append("--full")
        proc = subprocess.run(args, cwd=wiki_dir, capture_output=True,
                              text=True, timeout=1800)
        return {"returncode": proc.returncode}
    except Exception:
        return None  # best-effort; never break the hook


def mark_obsidian_vault(hook_input, reason="sessionEnd memory write"):
    """Mark the vault stale after this hook writes directly to memory.db."""
    try:
        wiki_dir = (hook_input.get("wiki_dir")
                    or os.environ.get("LTM_WIKI_DIR")
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki"))
        sync_py = os.path.join(wiki_dir, "sync.py")
        if not os.path.isfile(sync_py):
            return None
        proc = subprocess.run(
            [sys.executable, sync_py, "--mark", reason],
            cwd=wiki_dir, capture_output=True, text=True, timeout=30)
        return {"returncode": proc.returncode}
    except Exception:
        return None


def _memory_changed(session_result, agent_result):
    if session_result is not None:
        return True
    if not isinstance(agent_result, dict):
        return False
    return any(int(agent_result.get(key) or 0) > 0 for key in (
        "inserted", "topics_created", "superseded"))


def main():
    try: hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError): hook_input = {}
    try: session_result = process_session(hook_input)
    except Exception:
        session_result = None  # hooks must never crash
    # Pull in anything saved via the harness `store_memory` tool this session,
    # which otherwise lives only in the harness's own store and never reaches
    # the bridge. Must run BEFORE refresh_ltm_instructions so those facts are
    # included in the regenerated context.
    agent_result = sync_agent_memory(hook_input)
    if _memory_changed(session_result, agent_result):
        mark_obsidian_vault(hook_input)
    # After extracting new facts/entities, refresh the LTM instructions file
    # so the NEXT session starts with up-to-date memory context already baked in.
    refresh_ltm_instructions(hook_input)
    # Continuity: throttled, verified snapshot so RPO stays bounded automatically.
    auto_backup(hook_input)
    # Mirror everything this session learned into the Obsidian vault. Runs last
    # so it captures the facts extracted above, including the agent-memory sync.
    sync_obsidian_vault(hook_input)

if __name__ == "__main__":
    main()
