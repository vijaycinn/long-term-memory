"""
LTM wiki builder.

Compiles a cross-referenced Obsidian vault from the long-term memory database,
the CLI session store, and — optionally — a source workspace of markdown and
documents.

Idempotent. Every generated note carries `generated: true` in frontmatter;
the builder deletes only those before rebuilding, so hand-authored notes
placed anywhere in the vault survive a rebuild untouched.

Paths resolve through config.py (env vars → ~/.copilot/wiki-config.json →
defaults), so this runs unchanged on a machine that has never seen it.

Usage:  python build.py [--dry-run] [--memory-only]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import config

_P = config.paths()
# Source workspace is optional: without one the wiki is built purely from
# memory and the session store, which is the expected state on a new machine.
ROOT = _P["workspace"] or Path(os.getcwd())
HAS_WORKSPACE = _P["has_workspace"]
VAULT = _P["vault"]
BUILD = Path(__file__).resolve().parent
MEMDB = _P["memory_db"]
SESSDB = _P["session_db"]

NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y-%m-%d %H:%M UTC")
DRY = "--dry-run" in sys.argv
# Memory-only mode regenerates just the notes derived from memory.db and the
# session store, reusing the cached workspace inventory. A full build rescans
# ~950 files and takes over a minute, which is far too slow to run on every
# fact written; memory-only completes in a couple of seconds.
MEMORY_ONLY = "--memory-only" in sys.argv

# Build state belongs with the vault it describes, not with the code. A dot
# directory keeps it out of Obsidian's view, and means two vaults on one
# machine never share an inventory.
STATE_DIR = VAULT / ".wiki"
STATE = STATE_DIR / "state.json"
DIRTY = STATE_DIR / "dirty"
LOCK = STATE_DIR / "sync.lock"

# Which folders hold notes derived from memory/session data rather than files.
MEMORY_FOLDERS = {"accounts", "internal", "topics", "learnings", "sessions", "meta"}

F = {
    "meta": "00-Meta",
    "career": "01-Activity/Career",
    "territory": "01-Activity/Territory",
    "sessions": "01-Activity/Sessions",
    "accounts": "02-People/Accounts",
    "internal": "02-People/Internal",
    "research": "03-Research",
    "tech": "04-Tech",
    "topics": "05-Knowledge/Topics",
    "learnings": "05-Knowledge/Learnings",
    "briefs": "06-Outputs/Briefs",
    "posts": "06-Outputs/Posts",
    "tasks": "07-Tasks",
    "skills": "08-Skills",
    "reference": "09-Reference",
}

EXCLUDE_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "content_unpacked",
    "template_unpacked", "repo-backups", "_archive", "obsidian", ".playwright-mcp",
    ".copilot-work", "dist", ".swa-bin", ".obsidian",
}
EXCLUDE_PATH_RE = re.compile(
    r"(\\|/)(node_modules|\.git|__pycache__|\.venv|content_unpacked|template_unpacked"
    r"|repo-backups|_archive|\.skills-install-backup[^\\/]*|\.playwright-mcp)(\\|/)", re.I)

# (path regex, domain, note type, subject)
RULES = [
    (r"^docs[\\/]saif[\\/]",                        "accounts",  "account-doc", "SAIF"),
    (r"^docs[\\/]research[\\/]saif-",               "accounts",  "account-doc", "SAIF"),
    (r"^research[\\/]enghouse[\\/]",                "accounts",  "account-doc", "Enghouse"),
    (r"^files[\\/]linn-county",                     "accounts",  "account-doc", "Linn County"),
    (r"^docs[\\/]CFW_",                             "accounts",  "account-doc", "City of Fort Worth"),
    (r"^customer-draft[\\/]calix",                  "accounts",  "account-doc", "Calix"),
    (r"^cowork[\\/]seactivity[\\/]",                "territory", "se-activity", None),

    (r"^career[\\/]",                               "career",    "career",      None),
    (r"^docs[\\/]vof",                              "career",    "career",      None),
    (r"^docs[\\/]connect[\\/]",                     "career",    "career",      None),
    (r"^docs[\\/]FY26_Q4_Connect",                  "career",    "career",      None),
    (r"^research[\\/]linkedin-profile[\\/]",        "career",    "career",      None),
    (r"^research[\\/]internal-movement",            "career",    "career",      None),
    (r"^work-week[\\/]",                            "territory", "territory",   None),
    (r"^msx[\\/]",                                  "territory", "territory",   None),

    (r"^research[\\/]linkedin-posts[\\/]",          "posts",     "post",        None),
    (r"^drafts[\\/]",                               "briefs",    "draft",       None),
    (r"^customer-draft[\\/]",                       "briefs",    "draft",       None),
    (r"^files[\\/]",                                "briefs",    "draft",       None),

    (r"^research[\\/]",                             "research",  "research",    None),
    (r"^docs[\\/]research[\\/]",                    "research",  "research",    None),

    (r"^long-term-memory[\\/]",                     "tech",      "tech",        None),
    (r"^memory[\\/]",                               "tech",      "tech",        None),
    (r"^seismic-bridge[\\/]",                       "tech",      "tech",        None),
    (r"^aks-auto[\\/]",                             "tech",      "tech",        None),
    (r"^voice-agent[\\/]",                          "tech",      "tech",        None),
    (r"^contact-center-voice-demo[\\/]",            "tech",      "tech",        None),
    (r"^source[\\/]",                               "tech",      "tech",        None),
    (r"^_github_review[\\/]",                       "tech",      "tech",        None),
    (r"^skillhub[\\/]",                             "tech",      "tech",        None),
    (r"^install[\\/]",                              "tech",      "tech",        None),
    (r"^copilot-statusline[\\/]",                   "tech",      "tech",        None),
    (r"^build[\\/]",                                "tech",      "tech",        None),
    (r"^docs[\\/]",                                 "tech",      "tech",        None),

    (r"^\.github[\\/]",                             "meta",      "config",      None),
    (r"^memory-context\.md$",                       "meta",      "config",      None),

    (r"^anthropic[\\/]",                            "reference", "library",     "Anthropic Skill Library"),
    (r"^\.clinerules[\\/]",                         "reference", "library",     "Cline Rules Library"),
    (r"^copilot-skills-repo[\\/]",                  "reference", "mirror",      "Skills Repo Mirror"),
    (r"^copilot-customization[\\/]",                "reference", "mirror",      "Copilot Customization Backup"),
    (r"^cx-research-workspace[\\/]",                "reference", "eval",        "cx-research Evaluation"),
    (r"^doublecheck-workspace[\\/]",                "reference", "eval",        "doublecheck Evaluation"),
    (r"^demo-orchestrator[\\/]",                    "reference", "eval",        "demo-orchestrator Workspace"),
    (r"^samples[\\/]",                              "reference", "library",     "Samples"),
    (r"^scout[\\/]",                                "reference", "library",     "Scout Workspace"),
    (r"^skills[\\/]_",                              "reference", "library",     "Skill Shared Assets"),
    (r"^skills[\\/]",                               "reference", "library",     "Skill Reference Docs"),
]

ARTIFACT_EXT = {".html", ".csv", ".pptx", ".xlsx", ".pdf", ".docx", ".mmd"}
ARTIFACT_DIRS = {"msx", "work-week", "drafts", "files", "customer-draft", "career", "docs"}
MAX_IMPORT_BYTES = 500_000


# ---------------------------------------------------------------- utilities
def slugify(text: str, maxlen: int = 80) -> str:
    text = re.sub(r"[<>:\"/\\|?*\[\]#^]", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:maxlen]).strip(" .-") or "untitled"


def read_text(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def split_frontmatter(body: str):
    if not body.startswith("---"):
        return {}, body
    end = body.find("\n---", 3)
    if end == -1:
        return {}, body
    raw, rest = body[3:end], body[end + 4:].lstrip("\n")
    meta = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, rest


def title_from(path: Path, body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        t = re.sub(r"[#*`\[\]]", "", m.group(1)).strip()
        if 3 <= len(t) <= 110:
            return t
    return re.sub(r"[-_]+", " ", path.stem).strip().title()


def yl(items) -> str:
    items = [str(i).replace('"', "'") for i in items if i]
    if not items:
        return "[]"
    return "[" + ", ".join(f'"{i}"' for i in sorted(set(items))) + "]"


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def yaml_str(value: str) -> str:
    """Quote a YAML scalar safely.

    Single quotes are used because these values routinely contain Windows paths.
    In double-quoted YAML a backslash starts an escape sequence, so
    `"research\\graphrag.md"` is invalid YAML — `\\g` is not a legal escape. In
    single-quoted YAML nothing is escaped except a literal quote, which is
    written by doubling it.
    """
    s = str(value).replace("\n", " ").replace("\r", " ")
    return "'" + s.replace("'", "''") + "'"


def fm(**kw) -> str:
    out = ["---"]
    for k, v in kw.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (list, set, tuple)):
            out.append(f"{k}: {yl(v)}")
        elif isinstance(v, int):
            out.append(f"{k}: {v}")
        elif isinstance(v, str) and ISO_DATE.match(v):
            # Unquoted so Obsidian types it as a Date. Quoted it becomes text,
            # and every date filter and sort in Bases silently stops working.
            out.append(f"{k}: {v}")
        else:
            out.append(f"{k}: {yaml_str(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


def link(name: str) -> str:
    return f"[[{name}]]"


WRITTEN: list[Path] = []


MOC_FOR = {
    "meta": "Home",
    "career": "Activity MOC", "territory": "Activity MOC", "sessions": "Activity MOC",
    "accounts": "People MOC", "internal": "People MOC",
    "research": "Research MOC",
    "tech": "Tech MOC",
    "topics": "Knowledge MOC", "learnings": "Knowledge MOC",
    "briefs": "Outputs MOC", "posts": "Outputs MOC",
    "tasks": "Home",
    "skills": "Skills MOC",
    "reference": "Reference MOC",
}

# Notes are buffered rather than written immediately. Related-links are decided
# only once every note exists, so link density can be balanced globally instead
# of guessing per-note. flush_notes() does the writing.
QUEUE: list[dict] = []


def load_state() -> dict:
    """Inventory cached by the last full build.

    Memory-only rebuilds need to know the names of workspace-derived notes so
    they can still cross-link to them and list them in the maps of content,
    without paying to rescan the workspace.
    """
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"aliases": {}, "notes": [], "doc_subjects": {}, "skill_notes": {},
            "stamp": None}


def save_state(state: dict):
    if DRY:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def derive_summary(content: str, limit: int = 165) -> str:
    """One-line gist of a note, for index.md and Dataview.

    The index is what an agent reads first to decide which pages to open, so a
    summary that just repeats the note type is useless. Prefer the first real
    prose in the body, falling back to the callout only when there is nothing
    better.
    """
    body = content
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]
    callout = None
    for raw in body.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("#", "|", "---", "_", "[[", "```")):
            continue
        if ln.startswith(">"):
            text = re.sub(r"^>\s*(\[![a-z]+\][-+]?\s*)?", "", ln).strip()
            if text and callout is None:
                callout = text
            continue
        if ln.startswith(("- ", "* ")):
            ln = ln[2:].strip()
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", ln)
        ln = re.sub(r"[`*_]", "", ln).strip()
        if len(ln) >= 25:
            return (ln[:limit] + "…") if len(ln) > limit else ln
    if callout:
        return (callout[:limit] + "…") if len(callout) > limit else callout
    return ""


def write_note(folder_key: str, name: str, content: str, subdir: str = "",
               related: list | None = None, origin: str | None = None,
               summary: str | None = None) -> str:
    rel = F[folder_key] + (f"/{subdir}" if subdir else "")
    d = VAULT / rel
    stem = slugify(name)
    p = d / f"{stem}.md"
    n = 2
    while p in WRITTEN:
        p = d / f"{stem} ({n}).md"
        n += 1
    WRITTEN.append(p)
    QUEUE.append({"path": p, "folder": folder_key, "content": content,
                  "related": list(related or []), "stem": p.stem,
                  "summary": (summary or derive_summary(content)),
                  "origin": origin or ("memory" if folder_key in MEMORY_FOLDERS
                                       else "workspace")})
    return p.stem


def flush_notes(max_inbound: int = 45, verbose: bool = True,
                known_extra: set | None = None):
    """Write buffered notes, capping how many inbound Related edges any one
    note may collect.

    Without a cap, a handful of broadly-worded notes accumulate hundreds of
    inbound links and flatten the graph — every note ends up one hop from
    everything, which destroys the signal the graph is meant to carry. Edges are
    kept where the target is mentioned most, so specific relationships survive
    and incidental word matches are dropped.
    """
    inbound = {}
    for item in QUEUE:
        for t in item["related"]:
            inbound.setdefault(t, []).append(item["stem"])

    demoted = {t: len(v) for t, v in inbound.items() if len(v) > max_inbound}
    keep = {}
    for target, sources in inbound.items():
        if len(sources) <= max_inbound:
            keep[target] = set(sources)
        else:
            # keep the sources that mention the target most often
            scored = []
            for item in QUEUE:
                if item["stem"] in sources:
                    c = item["content"].lower().count(target.lower())
                    scored.append((c, item["stem"]))
            scored.sort(reverse=True)
            keep[target] = {s for _, s in scored[:max_inbound]}

    written = 0
    known = {p.stem for p in WRITTEN} | set(known_extra or ())
    # Imported source text (checkpoints, user asks, docs) can contain literal
    # [[...]] that is not a link. Escaping only *unresolved* targets keeps real
    # links intact while stopping phantom nodes appearing in the graph.
    unresolved = Counter()

    def escape_unresolved(text: str) -> str:
        def repl(m):
            target = m.group(1).split("|")[0].split("#")[0].strip()
            if target in known:
                return m.group(0)
            unresolved[target] += 1
            return "\u200b[\u200b[" + m.group(1) + "]]"
        return re.sub(r"\[\[([^\]\n]+)\]\]", repl, text)

    for item in QUEUE:
        rel = [t for t in item["related"] if item["stem"] in keep.get(t, ())]
        body = escape_unresolved(item["content"])
        # Stamp origin and summary so an incremental rebuild knows which notes it
        # owns, and so index.md can be built without re-reading every body.
        # A lambda replacement is required: summaries routinely contain Windows
        # paths, and re.sub would read those backslashes as escape sequences.
        stamp = f"generated: true\norigin: {item['origin']}"
        if item.get("summary"):
            stamp += "\nsummary: " + yaml_str(item["summary"])
        body = re.sub(r"^generated: true$", lambda _m: stamp, body, count=1, flags=re.M)
        if rel:
            body += ("\n\n## Related\n\n"
                     + "\n".join(f"- [[{r}]]" for r in sorted(set(rel))) + "\n")
        p = item["path"]
        moc = MOC_FOR.get(item["folder"])
        if moc and p.stem not in (moc, "Home") and body.startswith("---"):
            end = body.find("\n---", 3)
            if end != -1:
                cut = end + 4
                trail = f"[[Home]] › [[{moc}]]" if moc != "Home" else "[[Home]]"
                body = body[:cut] + f"\n{trail}\n" + body[cut:]
        if not DRY:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        written += 1
    if verbose and unresolved:
        print(f"[links] escaped {sum(unresolved.values())} literal [[..]] in source text "
              f"({len(unresolved)} distinct)")
    if verbose and demoted:
        top = sorted(demoted.items(), key=lambda x: -x[1])[:10]
        print(f"[links] capped inbound Related edges on {len(demoted)} hub notes "
              f"(max {max_inbound})")
        for t, n in top:
            print(f"        {t}: {n} -> {max_inbound}")
    return written


def classify(rel: str):
    for pattern, domain, ntype, subject in RULES:
        if re.search(pattern, rel, re.I):
            return domain, ntype, subject
    return "reference", "misc", "Unsorted"


# ------------------------------------------------------------------- scan
def scan():
    """Walk the source workspace. Returns empty lists when none is configured."""
    md, artifacts, skillfiles = [], [], []
    if not HAS_WORKSPACE:
        return md, artifacts, skillfiles
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS
                       and not d.startswith(".skills-install-backup")]
        if EXCLUDE_PATH_RE.search(dirpath + os.sep):
            continue
        rel_dir = os.path.relpath(dirpath, ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            ext = full.suffix.lower()
            if ext == ".md":
                if fn == "SKILL.md" and re.match(r"^skills[\\/][^\\/_]", rel, re.I):
                    skillfiles.append((rel, full))
                else:
                    md.append((rel, full))
            elif ext in ARTIFACT_EXT and rel.split(os.sep)[0] in ARTIFACT_DIRS:
                artifacts.append((rel, full))
    return md, artifacts, skillfiles


def load_memory():
    if not MEMDB.exists():
        return {k: [] for k in ("topics", "facts", "entities", "snapshots", "sessions", "patterns")}
    c = sqlite3.connect(f"file:{MEMDB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    q = lambda s: [dict(r) for r in c.execute(s)]
    out = {
        "topics": q("select * from topics"),
        "facts": q("select * from facts where coalesce(status,'active') <> 'superseded'"),
        "entities": q("select * from entities"),
        "snapshots": q("select * from snapshots"),
        "sessions": q("select * from ltm_session_log order by processed_at desc"),
        "patterns": q("select * from ltm_patterns order by created_at desc"),
    }
    c.close()
    return out


def load_session_store():
    """The CLI session store holds the real working record — session titles,
    checkpoint narratives and the files each session touched. memory.db only
    keeps the sessions that produced durable facts, so this is the wider view.

    Optional: a machine that has never run the CLI simply has no store, and the
    wiki is built without the activity layer rather than failing.
    """
    empty = {"sessions": [], "checkpoints": [], "files": [], "turns": []}
    if not SESSDB.exists():
        return empty
    try:
        c = sqlite3.connect(f"file:{SESSDB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        out = {
            "sessions": [dict(r) for r in c.execute(
                "select id, cwd, repository, summary, created_at, updated_at "
                "from sessions order by created_at desc")],
            "checkpoints": [dict(r) for r in c.execute(
                "select session_id, checkpoint_number, title, overview, work_done, "
                "technical_details, important_files, next_steps, created_at "
                "from checkpoints order by created_at desc")],
            "files": [dict(r) for r in c.execute(
                "select session_id, file_path, tool_name, first_seen_at from session_files")],
            "turns": [dict(r) for r in c.execute(
                "select session_id, turn_index, user_message, timestamp from turns "
                "where turn_index = 0")],
        }
        c.close()
        return out
    except Exception as e:                                   # pragma: no cover
        print(f"[warn] session store unreadable: {e}")
        return empty


# ------------------------------------------------------------ vault reset
def clear_generated(origin: str | None = None):
    """Delete previously generated notes so they can be rebuilt.

    Only notes carrying `generated: true` are touched — hand-authored notes
    always survive. When `origin` is given, only notes stamped with that origin
    are removed, which is what lets a memory-only rebuild leave the
    workspace-derived notes in place.
    """
    removed = 0
    if not VAULT.exists():
        return 0
    for p in VAULT.rglob("*.md"):
        if ".obsidian" in p.parts:
            continue
        head = read_text(p)[:400]
        if not re.search(r"^generated:\s*true\s*$", head, re.M):
            continue
        if origin:
            m = re.search(r"^origin:\s*(\w+)\s*$", head, re.M)
            # Notes written before origin stamping existed are treated as
            # workspace-owned, so an incremental run never silently drops them.
            if (m.group(1) if m else "workspace") != origin:
                continue
        if not DRY:
            p.unlink()
        removed += 1
    if not DRY and not origin:
        for d in sorted((VAULT / v for v in F.values()), key=lambda x: -len(str(x))):
            for sub in sorted(d.rglob("*"), key=lambda x: -len(str(x))):
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
    return removed
