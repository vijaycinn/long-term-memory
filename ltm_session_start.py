#!/usr/bin/env python3
"""LTM Session-Start Hook — loads long-term memory context into the Copilot CLI context window.

Runs as a sessionStart hook. Reads memory.db + session-store.db (read-only) and writes
~/.copilot/instructions/ltm.instructions.md with YAML frontmatter so Copilot auto-loads it.

Input (stdin): JSON {timestamp, cwd, source, initialPrompt}
Output: None (writes to file; stdout is ignored by sessionStart hooks)
"""

import json
import os
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


def _section_high_priority_facts(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT f.content, f.fact_type, f.source, t.slug AS topic_slug
           FROM facts f LEFT JOIN topics t ON t.id = f.topic_id
           WHERE f.status='active' AND f.importance >= 4
           ORDER BY f.importance DESC, f.last_accessed_at DESC
           LIMIT 20"""
    ).fetchall()
    if not rows:
        return []
    by_topic: dict[str, list] = {}
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
    sections: list[list[str]] = [
        _section_identity(conn),
        _section_active_topics(conn),
        _section_high_priority_facts(conn),
        _section_pending_work(conn),
        _section_recent_patterns(conn),
        _section_recent_sessions(cwd, repo_id, store_override=store_override),
        _section_known_entities(conn),
        _section_staleness_nudge(conn),
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
