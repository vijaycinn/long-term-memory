"""index.md and log.md — the two navigation files the LLM-wiki pattern relies on.

`index.md` is content-oriented: a catalogue of every page with a one-line gist,
read first so an agent can pick the handful of pages that matter instead of
scanning the vault. `log.md` is chronological and append-only.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from vaultlib import DRY, NOW, STAMP, VAULT, read_text

# Accepts either quote style: values containing Windows paths must be
# single-quoted in YAML, so both forms appear in frontmatter.
SUMMARY_RE = re.compile(r'''^summary:\s*(?:"(.*)"|'(.*)')\s*$''', re.M)


SECTION_BLURB = {
    "00-Meta": "How the vault works, identity, build record",
    "01-Activity": "What happened — work log, sessions, territory, career",
    "02-People": "Who — accounts and people with their known facts",
    "03-Research": "What we investigated",
    "04-Tech": "How the systems work",
    "05-Knowledge": "What we know — topics and distilled learnings",
    "06-Outputs": "What we shipped",
    "07-Tasks": "What is unfinished",
    "08-Skills": "What we can do",
    "09-Reference": "External material, indexed not copied",
    "10-Synthesis": "LLM-written analysis and answers filed back",
}
ORDER = list(SECTION_BLURB)


def _scan_vault():
    """Every page on disk with its folder and frontmatter summary."""
    pages = []
    for p in sorted(VAULT.rglob("*.md")):
        if ".obsidian" in p.parts or p.name in ("index.md", "log.md"):
            continue
        rel = p.relative_to(VAULT).as_posix()
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        head = read_text(p)[:1500]
        m = SUMMARY_RE.search(head)
        summary = ""
        if m:
            summary = (m.group(1) if m.group(1) is not None else m.group(2) or "")
            summary = summary.replace("''", "'")
        generated = bool(re.search(r"^generated:\s*true\s*$", head, re.M))
        pages.append({"stem": p.stem, "folder": folder,
                      "summary": summary,
                      "generated": generated})
    return pages


def write_index():
    """Regenerate index.md. Returns the page count."""
    pages = _scan_vault()
    by_folder = defaultdict(list)
    for pg in pages:
        by_folder[pg["folder"]].append(pg)

    lines = [
        "---",
        'title: "Index"',
        'type: "index"',
        "generated: true",
        "origin: index",
        f'updated: "{NOW:%Y-%m-%d}"',
        "---",
        "",
        "# Index",
        "",
        "> [!abstract] Catalogue of every page in this vault, one line each.",
        "> **Read this first.** Find the pages that matter, then open only those —",
        "> do not scan the vault and do not re-derive what is already written here.",
        "> The operating contract is [[AGENTS]]; the chronology is [[log]].",
        "",
        f"**{len(pages)} pages** · regenerated {STAMP}",
        "",
        "| Section | Pages | What lives here |",
        "|---|--:|---|",
    ]
    seen_roots = []
    for root in ORDER:
        keys = [k for k in by_folder if k == root or k.startswith(root + "/")]
        n = sum(len(by_folder[k]) for k in keys)
        if n:
            lines.append(f"| `{root}` | {n} | {SECTION_BLURB.get(root, '')} |")
            seen_roots.append(root)
    loose = [k for k in by_folder if not any(
        k == r or k.startswith(r + "/") for r in ORDER)]
    if loose:
        n = sum(len(by_folder[k]) for k in loose)
        lines.append(f"| _(root)_ | {n} | Vault-level pages |")
    lines.append("")

    for root in seen_roots:
        keys = sorted(k for k in by_folder if k == root or k.startswith(root + "/"))
        lines.append(f"\n## {root}\n")
        for k in keys:
            group = sorted(by_folder[k], key=lambda x: x["stem"].lower())
            if not group:
                continue
            if k != root:
                lines.append(f"\n### {k.split('/', 1)[1]}\n")
            for pg in group:
                mark = "" if pg["generated"] else " _(hand-authored)_"
                s = pg["summary"]
                lines.append(f"- [[{pg['stem']}]]{mark}" + (f" — {s}" if s else ""))
    if loose:
        lines.append("\n## Vault root\n")
        for k in sorted(loose):
            for pg in sorted(by_folder[k], key=lambda x: x["stem"].lower()):
                s = pg["summary"]
                lines.append(f"- [[{pg['stem']}]]" + (f" — {s}" if s else ""))

    lines.append("\n---\n\n_Regenerated on every build. Nothing here is hand-edited; "
                 "edit the page itself, not this catalogue._\n")
    if not DRY:
        (VAULT / "index.md").write_text("\n".join(lines), encoding="utf-8")
    return len(pages)


LOG_HEADER = """---
title: "Log"
type: "log"
---

# Log

> [!info] Append-only chronology of what happened to this vault — builds,
> ingests, answers filed back, lint passes. Never rewritten, so it stays a
> reliable record of how the knowledge here evolved.
>
> Recent activity: `grep "^## \\[" log.md | tail -5`

"""


def append_log(entry_type: str, title: str, detail: str = ""):
    """Append one entry to log.md.

    The fixed `## [date] type | title` prefix is deliberate: it makes the log
    greppable with plain unix tools, so "what happened lately" costs one command
    rather than reading the file.
    """
    if DRY:
        return
    log = VAULT / "log.md"
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(LOG_HEADER, encoding="utf-8")
    entry = f"## [{NOW:%Y-%m-%d %H:%M}] {entry_type} | {title}\n"
    if detail:
        entry += f"\n{detail}\n"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(entry + "\n")
