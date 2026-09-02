#!/usr/bin/env python3
"""LTM Session-Start Hook — loads long-term memory context into the Copilot CLI context window.

Runs as a sessionStart hook. Reads memory.db + session-store.db (read-only) and writes
~/.copilot/instructions/ltm.instructions.md with YAML frontmatter so Copilot auto-loads it.

Input (stdin): JSON {timestamp, cwd, source, initialPrompt}
Output: None (writes to file; stdout is ignored by sessionStart hooks)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = os.path.join(
    os.environ.get("USERPROFILE", str(Path.home())), ".copilot", "memory.db"
)
# Copilot CLI reads user-level instructions from this single file:
OUTPUT_FILE = Path(
    os.environ.get("USERPROFILE", str(Path.home()))
) / ".copilot" / "copilot-instructions.md"

LTM_START_MARKER = "<!-- LTM-START -->"
LTM_END_MARKER = "<!-- LTM-END -->"

# A short prompt is usually a command fragment, not enough context to retrieve
# useful memory. Keep the old block exactly in that case as a reliable rollback.
INITIAL_PROMPT_MIN_CHARS = 15
HIGH_PRIORITY_FACT_LIMIT = 20

# Keep half of the load-bearing fact block fixed and let the other half follow
# the session's first request. These are tuning knobs, not incidental limits.
FIXED_IMPORTANCE_FIVE_FACT_LIMIT = 10
PROMPT_RELEVANT_FACT_LIMIT = 10

_PROMPT_STOP_WORDS = {
    "about", "also", "and", "are", "can", "find", "for", "from", "have", "how",
    "into", "need", "please", "should", "that", "the", "this", "what", "with",
}


# ── DB helpers ────────────────────────────────────────────────────────

def get_conn_readonly(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection (same WAL/row_factory as memory_driver)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def find_session_store() -> Path | None:
    candidates = [Path.home() / ".copilot" / "session-store.db"]
    ch = os.environ.get("COPILOT_HOME")
    if ch:
        candidates.append(Path(ch) / "session-store.db")
    return next((p for p in candidates if p.exists()), None)


def get_repo_identifier(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if r.returncode == 0:
            url = r.stdout.strip()
            for pfx in [
                "https://github.com/", "git@github.com:",
                "https://dev.azure.com/", "git@ssh.dev.azure.com:v3/",
            ]:
                if url.startswith(pfx):
                    url = url[len(pfx):]
            return url.removesuffix(".git").strip("/")
    except Exception:
        pass
    return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Section builders ──────────────────────────────────────────────────

def _section_identity(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute(
        "SELECT name, entity_type, attributes, notes FROM entities WHERE is_self=1 LIMIT 1"
    ).fetchone()
    if not row:
        return []
    lines = ["## 🧠 Identity", ""]
    lines.append(f"- **Name**: {row['name']}")
    if row["attributes"]:
        lines.append(f"- **Attributes**: {row['attributes']}")
    if row["notes"]:
        lines.append(f"- **Notes**: {row['notes']}")
    lines.append("")
    return lines


def _section_active_topics(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT slug, title, category, last_accessed_at
           FROM topics WHERE status='active'
           ORDER BY last_accessed_at DESC LIMIT 5"""
    ).fetchall()
    if not rows:
        return []
    lines = ["## 📂 Active Topics", ""]
    for r in rows:
        accessed = r["last_accessed_at"] or "never"
        lines.append(f"- **{r['title']}** (`{r['slug']}`) — {r['category']} — last: {accessed}")
    lines.append("")
    return lines


def _high_priority_fact_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT f.id, f.content, f.fact_type, f.source, t.slug AS topic_slug
           FROM facts f LEFT JOIN topics t ON t.id = f.topic_id
           WHERE f.status='active' AND f.importance >= 4
           ORDER BY f.importance DESC, f.last_accessed_at DESC
           LIMIT ?""",
        (HIGH_PRIORITY_FACT_LIMIT,),
    ).fetchall()
    return rows


def _render_high_priority_facts(rows: list[sqlite3.Row | dict]) -> list[str]:
    if not rows:
        return []
    by_topic: dict[str, list[sqlite3.Row | dict]] = {}
    for r in rows:
        slug = r["topic_slug"] or "general"
        by_topic.setdefault(slug, []).append(r)
    lines = ["## ⭐ High-Priority Facts", ""]
    for slug, facts in by_topic.items():
        lines.append(f"**[{slug}]**")
        for f in facts:
            src = f" _(src: {f['source']})_" if f["source"] else ""
            lines.append(f"- [{f['fact_type']}] {f['content']}{src}")
    lines.append("")
    return lines


def _section_high_priority_facts(conn: sqlite3.Connection) -> list[str]:
    """Render the legacy fixed high-priority block."""
    return _render_high_priority_facts(_high_priority_fact_rows(conn))


def _log_retrieval_failure(exc: Exception) -> None:
    """Keep hook failures visible without allowing them to break session start."""
    print(f"ltm_session_start: prompt fact retrieval failed: {exc}", file=sys.stderr)


def _prompt_search_query(prompt: str) -> str:
    """Turn natural language into one broad FTS query without reimplementing FTS."""
    terms = [
        term for term in re.findall(r"\w{3,}", prompt.lower())
        if term not in _PROMPT_STOP_WORDS
    ]
    return " OR ".join(dict.fromkeys(terms)) or prompt


def _section_prompt_aware_high_priority_facts(
    conn: sqlite3.Connection, prompt: str, db_path: str
) -> list[str]:
    """Replace half the fixed block with relevant facts without expanding it."""
    legacy_rows = _high_priority_fact_rows(conn)
    legacy_lines = _render_high_priority_facts(legacy_rows)
    if not legacy_rows:
        return legacy_lines

    try:
        # Import lazily: a missing or incompatible driver must not prevent the
        # unconditional session-start context from being written.
        core_dir = str(Path(__file__).resolve().parent.parent / "core")
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)
        from memory_driver import search_memory

        fixed_rows = conn.execute(
            """SELECT f.id, f.content, f.fact_type, f.source, t.slug AS topic_slug
               FROM facts f LEFT JOIN topics t ON t.id = f.topic_id
               WHERE f.status='active' AND f.importance >= 5
               ORDER BY f.importance DESC, f.last_accessed_at DESC
               LIMIT ?""",
            (FIXED_IMPORTANCE_FIVE_FACT_LIMIT,),
        ).fetchall()
        fixed_ids = {row["id"] for row in fixed_rows}

        # search_memory owns the FTS5 punctuation retry. It is deliberately
        # called rather than duplicating that query logic in this hook.
        search_rows = search_memory({
            # FTS5 treats whitespace as AND. Natural-language filler would
            # otherwise eliminate the fact named by a focused user request.
            "query": _prompt_search_query(prompt),
            "limit": PROMPT_RELEVANT_FACT_LIMIT * 3,
            "db_path": db_path,
        })
        relevant_rows = [
            {
                "id": row["id"],
                "content": row["content"],
                "fact_type": row["subtype"],
                "source": "",
                "topic_slug": "retrieved",
            }
            for row in search_rows
            if row["src"] == "facts" and row["id"] not in fixed_ids
        ]
        relevant_ids = {row["id"] for row in relevant_rows}

        selected: list[sqlite3.Row | dict] = list(fixed_rows)
        selected_ids = set(fixed_ids)
        for row in relevant_rows:
            if len(selected) >= FIXED_IMPORTANCE_FIVE_FACT_LIMIT + PROMPT_RELEVANT_FACT_LIMIT:
                break
            if row["id"] not in selected_ids:
                selected.append(row)
                selected_ids.add(row["id"])

        # Preserve the original block's usefulness if search has too few facts.
        for row in legacy_rows:
            if len(selected) >= HIGH_PRIORITY_FACT_LIMIT:
                break
            if row["id"] not in selected_ids:
                selected.append(row)
                selected_ids.add(row["id"])

        # Relevant facts may be longer than the fixed facts they displaced.
        # Keep the legacy count, then trim only retrieved content if required
        # so the complete injected context remains within the old budget.
        budget = len("\n".join(legacy_lines))
        lines = _render_high_priority_facts(selected)
        for row in selected:
            if len("\n".join(lines)) <= budget:
                break
            if not isinstance(row, dict) or row["id"] not in relevant_ids:
                continue
            excess = len("\n".join(lines)) - budget
            content = row["content"]
            keep = max(1, len(content) - excess - 3)
            row["content"] = content[:keep] + "..."
            lines = _render_high_priority_facts(selected)
        if len("\n".join(lines)) > budget:
            # The fixed block always fits this budget; returning it is safer
            # than allowing a session-start injection to grow unexpectedly.
            return legacy_lines
        return lines
    except Exception as exc:
        _log_retrieval_failure(exc)
        return legacy_lines


def _section_pending_work(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT content FROM facts
           WHERE fact_type='todo' AND status='active'
           ORDER BY importance DESC, created_at DESC
           LIMIT 10"""
    ).fetchall()
    if not rows:
        return []
    lines = ["## ✅ Pending Work", ""]
    for r in rows:
        lines.append(f"- [ ] {r['content']}")
    lines.append("")
    return lines


def _section_recent_patterns(conn: sqlite3.Connection) -> list[str]:
    # ltm_patterns may not exist yet (created by session-end hook)
    try:
        rows = conn.execute(
            """SELECT pattern_type, COUNT(*) AS cnt
               FROM ltm_patterns
               GROUP BY pattern_type
               ORDER BY MAX(created_at) DESC
               LIMIT 5"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    lines = ["## 🔄 Recent Patterns", ""]
    for r in rows:
        lines.append(f"- **{r['pattern_type']}** × {r['cnt']}")
    lines.append("")
    return lines


def _section_recent_sessions(
    cwd: str, repo_id: str | None, *, store_override: Path | None = None
) -> list[str]:
    store_path = store_override or find_session_store()
    if not store_path or not store_path.exists():
        return []
    try:
        sc = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True, timeout=5)
        sc.row_factory = sqlite3.Row
    except Exception:
        return []

    dirname = _escape_like(Path(cwd).name) if cwd else ""
    try:
        if repo_id and dirname:
            sessions = sc.execute(
                """SELECT id, cwd, repository, branch, summary, created_at
                   FROM sessions
                   WHERE repository = ? OR cwd LIKE ? ESCAPE '\\'
                   ORDER BY created_at DESC LIMIT 3""",
                (repo_id, f"%{dirname}%"),
            ).fetchall()
        elif repo_id:
            sessions = sc.execute(
                """SELECT id, cwd, repository, branch, summary, created_at
                   FROM sessions WHERE repository = ?
                   ORDER BY created_at DESC LIMIT 3""",
                (repo_id,),
            ).fetchall()
        elif dirname:
            sessions = sc.execute(
                """SELECT id, cwd, repository, branch, summary, created_at
                   FROM sessions WHERE cwd LIKE ? ESCAPE '\\'
                   ORDER BY created_at DESC LIMIT 3""",
                (f"%{dirname}%",),
            ).fetchall()
        else:
            sc.close()
            return []

        # Get first user message for each session
        result_lines: list[str] = []
        for s in sessions:
            first = sc.execute(
                "SELECT user_message FROM turns WHERE session_id=? AND turn_index=0",
                (s["id"],),
            ).fetchone()
            summary = (s["summary"] or "")[:120]
            branch = s["branch"] or ""
            ask = ((first["user_message"] or "")[:100] if first else "")
            parts = []
            if summary:
                parts.append(summary)
            if branch:
                parts.append(f"branch: `{branch}`")
            if ask:
                parts.append(f"started: _{ask}_")
            if parts:
                result_lines.append(f"- {' | '.join(parts)}")
        sc.close()
    except Exception:
        try:
            sc.close()
        except Exception:
            pass
        return []

    if not result_lines:
        return []
    lines = ["## 📋 Recent Session Context", ""]
    lines.extend(result_lines)
    lines.append("")
    return lines


def _section_known_entities(conn: sqlite3.Connection) -> list[str]:
    # Top 10 most recently mentioned entities via entity_mentions
    try:
        rows = conn.execute(
            """SELECT DISTINCT e.name, e.entity_type
               FROM entity_mentions em
               JOIN entities e ON e.id = em.entity_id
               WHERE e.is_self = 0
               ORDER BY em.created_at DESC
               LIMIT 10"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        # Fallback: most recently updated entities
        rows = conn.execute(
            """SELECT name, entity_type FROM entities
               WHERE is_self = 0
               ORDER BY updated_at DESC LIMIT 10"""
        ).fetchall()
    if not rows:
        return []
    lines = ["## 👥 Known Entities", ""]
    for r in rows:
        lines.append(f"- {r['name']} ({r['entity_type']})")
    lines.append("")
    return lines


def _section_embed_auth_nudge() -> list[str]:
    """Warn when the Azure login backing semantic search has lapsed.

    Deliberately does NOT shell out to `az`: that costs ~1-2 s and this hook runs
    on every session start under a 10,000 ms budget where everything else is
    ~40 ms. wiki/vault_embed.py caches the token together with its expiry, so
    this is a single small file read.

    Silent when semantic search is not configured at all — a user who never set
    an embedding endpoint should never be nagged about a login they do not need.
    """
    try:
        wiki_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "wiki")
        if wiki_dir not in sys.path:
            sys.path.insert(0, wiki_dir)
        import vault_embed
        st = vault_embed.token_status()
    except Exception:
        # Never let an optional nudge break session start.
        return []

    if not st.get("configured") or st.get("state") == "ok":
        return []

    if st.get("state") == "missing":
        detail = "no cached Azure token was found"
    else:
        mins = max(0, st.get("expires_in", 0)) // 60
        detail = (f"the cached Azure token expires in ~{mins} min"
                  if st.get("expires_in", 0) > 0 else "the cached Azure token has expired")
    return [
        f"> ⚠️ **Semantic (vector) search is degraded** — {detail}.",
        "> Run **`az login`** to restore it. Until then, vault search silently",
        "> falls back to lexical matching, which cannot match paraphrased wording.",
        "",
    ]


def _section_staleness_nudge(conn: sqlite3.Connection) -> list[str]:
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM facts
               WHERE importance >= 3 AND status = 'active'
                 AND last_accessed_at IS NOT NULL
                 AND last_accessed_at < datetime('now', '-30 days')"""
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row or row["cnt"] == 0:
        return []
    return [
        f"> 💡 **{row['cnt']}** important facts haven't been accessed in 30+ days. "
        "Consider reviewing with `Search-Memory` or archiving stale items.",
        "",
    ]


# ── Orchestrator ──────────────────────────────────────────────────────

def generate_instructions(
    hook_input: dict,
    *,
    memory_db: str | None = None,
    store_override: Path | None = None,
    output_path: Path | None = None,
) -> str | None:
    """Build the instructions markdown and write it. Returns the content or None."""
    cwd = hook_input.get("cwd", os.getcwd())
    db_path = memory_db or DEFAULT_DB
    out = output_path or OUTPUT_FILE

    # If memory.db doesn't exist, write a minimal setup pointer
    if not os.path.exists(db_path):
        content = _minimal_instructions("memory.db not found — run `init-memory.sql` to set up LTM.")
        _write_file(out, content)
        return content

    try:
        conn = get_conn_readonly(db_path)
    except Exception:
        content = _minimal_instructions("Could not open memory.db.")
        _write_file(out, content)
        return content

    # Check if DB has any data
    try:
        count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        count += conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        count += conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        content = _minimal_instructions("memory.db schema not initialized — run `init-memory.sql`.")
        _write_file(out, content)
        return content

    if count == 0:
        conn.close()
        content = _minimal_instructions(
            "Memory is empty. Use `Add-Fact`, `Add-Topic`, or `Add-Entity` to populate."
        )
        _write_file(out, content)
        return content

    repo_id = get_repo_identifier(cwd) if os.path.isdir(cwd) else None

    # Build all sections
    initial_prompt = str(hook_input.get("initialPrompt") or "").strip()
    if len(initial_prompt) < INITIAL_PROMPT_MIN_CHARS:
        fact_section = _section_high_priority_facts(conn)
    else:
        fact_section = _section_prompt_aware_high_priority_facts(conn, initial_prompt, db_path)
    sections: list[list[str]] = [
        _section_identity(conn),
        _section_active_topics(conn),
        fact_section,
        _section_pending_work(conn),
        _section_recent_patterns(conn),
        _section_recent_sessions(cwd, repo_id, store_override=store_override),
        _section_known_entities(conn),
        _section_staleness_nudge(conn),
        # Last, so an actionable "your login lapsed" line is the closest thing
        # to the conversation rather than buried above recent-session history.
        _section_embed_auth_nudge(),
    ]
    conn.close()

    body_lines: list[str] = []
    for section in sections:
        if section:
            body_lines.extend(section)

    if not body_lines:
        content = _minimal_instructions("Memory exists but nothing relevant to load.")
        _write_file(out, content)
        return content

    header = [
        "# Long-Term Memory Context",
        f"<!-- Generated: {_now_utc()} -->",
        "",
        "_Persistent working memory — loaded automatically at session start._",
        "",
    ]
    footer = [
        "---",
        "_Commands: `Search-Memory -Query 'term'` · `Add-Fact` · `Add-Topic` · `Add-Entity`_",
    ]
    content = "\n".join(header + body_lines + footer)
    _write_file(out, content)
    return content


def _minimal_instructions(msg: str) -> str:
    return "\n".join([
        "# Long-Term Memory",
        "",
        f"> ⚠️ {msg}",
        "",
    ])


def _write_file(path: Path, content: str) -> None:
    """Merge LTM content into copilot-instructions.md, preserving other sections."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")

    tagged = f"{LTM_START_MARKER}\n{content}\n{LTM_END_MARKER}"

    if LTM_START_MARKER in existing and LTM_END_MARKER in existing:
        # Replace the existing LTM block
        before = existing[: existing.index(LTM_START_MARKER)]
        after = existing[existing.index(LTM_END_MARKER) + len(LTM_END_MARKER) :]
        merged = before.rstrip("\n") + "\n\n" + tagged + after.lstrip("\n")
    elif existing.strip():
        # Append LTM block after existing content
        merged = existing.rstrip("\n") + "\n\n" + tagged + "\n"
    else:
        merged = tagged + "\n"

    path.write_text(merged, encoding="utf-8")


# ── Entrypoint ────────────────────────────────────────────────────────

def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        hook_input = {}
    try:
        generate_instructions(hook_input)
    except Exception:
        pass  # hooks must never crash


if __name__ == "__main__":
    main()
