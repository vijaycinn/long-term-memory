"""wiki.py — operate the vault the way the LLM-wiki pattern expects.

The generated layer is compiled from sources and is rebuilt wholesale. This CLI
covers the other half: knowledge that originates in a conversation. An analysis
you produced, a comparison you were asked for, a connection you noticed — those
are new knowledge, and without somewhere to put them they evaporate into chat
history and the vault plateaus.

Pages written here carry no `generated: true`, so the builder never touches them.

  python wiki.py file-answer "Title" --body-file ans.md --tags saif,pricing
  python wiki.py file-answer "Title" --body "..."   --question "what was asked"
  python wiki.py log ingest "Article title" --detail "what changed"
  python wiki.py recent            # last log entries
  python wiki.py search "term"     # grep the vault, ranked
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Accepts either quote style: values containing Windows paths must be
# single-quoted in YAML, so both forms appear in frontmatter.
SUMMARY_RE = re.compile(r'''^summary:\s*(?:"(.*)"|'(.*)')\s*$''', re.M)

import config

VAULT = config.paths()["vault"]
BUILD = Path(__file__).resolve().parent
SYNTH = VAULT / "10-Synthesis"
NOW = datetime.now(timezone.utc)


def _slug(text: str, maxlen: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\[\]#^]', " ", text)
    return re.sub(r"\s+", " ", text).strip()[:maxlen].strip(" .-") or "untitled"


def _read(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _existing_pages():
    return {p.stem for p in VAULT.rglob("*.md") if ".obsidian" not in p.parts}


def _autolink(body: str, self_stem: str, limit: int = 20) -> list[str]:
    """Suggest wikilinks by matching existing page names in the body.

    Generated pages carry type prefixes (`Topic - x`, `Skill - y`), so the bare
    subject is matched too — prose says "Content Understanding Pricing", not
    "Topic - Content Understanding Pricing". Four characters is the floor
    because real account names (SAIF, MSX) are that short.
    """
    aliases: dict[str, str] = {}
    for name in _existing_pages() - {self_stem}:
        for candidate in (name, re.sub(r"^(Topic|Skill|Library|Artifacts) - ", "", name)):
            c = candidate.strip().lower()
            if len(c) >= 4:
                aliases.setdefault(c, name)
    low = body.lower()
    hits = Counter()
    for alias, name in aliases.items():
        if re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", low):
            hits[name] += low.count(alias)
    return [n for n, _ in hits.most_common(limit)]


def _derive_summary(body: str, limit: int = 165) -> str:
    """One-line gist for index.md — first real prose in the body."""
    for raw in body.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("#", "|", "---", ">", "[[", "```", "_")):
            continue
        if ln.startswith(("- ", "* ")):
            ln = ln[2:].strip()
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", ln)
        ln = re.sub(r"[`*_]", "", ln).strip()
        if len(ln) >= 25:
            return (ln[:limit] + "…") if len(ln) > limit else ln
    return ""


def cmd_file_answer(a) -> int:
    body = a.body
    if a.body_file:
        f = Path(a.body_file)
        if not f.is_file():
            print(f"error: --body-file not found: {f}", file=sys.stderr)
            return 2
        body = _read(f)
    if not body or not body.strip():
        print("error: provide --body or --body-file", file=sys.stderr)
        return 2

    SYNTH.mkdir(parents=True, exist_ok=True)
    stem = _slug(a.title)
    path = SYNTH / f"{stem}.md"
    n = 2
    while path.exists() and not a.overwrite:
        path = SYNTH / f"{stem} ({n}).md"
        n += 1

    tags = ["synthesis"] + [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    related = _autolink(body, path.stem)

    fm = ["---", f'title: "{a.title}"', 'type: "synthesis"',
          f'created: "{NOW:%Y-%m-%d}"',
          "tags: [" + ", ".join(f'"{t}"' for t in dict.fromkeys(tags)) + "]"]
    summary = (a.summary or _derive_summary(body))
    if summary:
        fm.append("summary: '" + summary.replace("'", "''") + "'")
    if a.question:
        fm.append(f'question: "{a.question.replace(chr(34), chr(39))}"')
    if a.sources:
        srcs = [s.strip() for s in a.sources.split(",") if s.strip()]
        fm.append("sources: [" + ", ".join(f'"{s}"' for s in srcs) + "]")
    fm.append("---")

    out = ["\n".join(fm), "", "[[Home]] › [[index]]", "", f"# {a.title}", ""]
    if a.question:
        out += [f"> [!question] Asked\n> {a.question}", ""]
    out += [body.strip(), ""]
    if related:
        out += ["", "## Related", ""] + [f"- [[{r}]]" for r in sorted(related)] + [""]
    out += ["---", "",
            f"_Filed {NOW:%Y-%m-%d %H:%M UTC}. Hand-authored — the builder never "
            f"rewrites this page._", ""]

    path.write_text("\n".join(out), encoding="utf-8")
    _append_log("answer", a.title,
                f"- filed to `{path.relative_to(VAULT).as_posix()}`\n"
                + (f"- question: {a.question}\n" if a.question else "")
                + (f"- linked: {', '.join(related[:8])}\n" if related else ""))
    print(f"filed  → {path}")
    print(f"linked → {len(related)} page(s)")
    print("note: run `python build.py --memory-only` to refresh index.md")
    return 0


LOG_HEADER = """---
title: "Log"
type: "log"
---

# Log

> [!info] Append-only chronology of what happened to this vault — builds,
> ingests, answers filed back, lint passes. Never rewritten.
>
> Recent activity: `grep "^## \\[" log.md | tail -5`

"""


def _append_log(entry_type: str, title: str, detail: str = ""):
    log = VAULT / "log.md"
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(LOG_HEADER, encoding="utf-8")
    entry = f"## [{NOW:%Y-%m-%d %H:%M}] {entry_type} | {title}\n"
    if detail:
        entry += f"\n{detail}\n"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(entry + "\n")


def cmd_log(a) -> int:
    _append_log(a.type, a.title, a.detail or "")
    print(f"logged: [{NOW:%Y-%m-%d %H:%M}] {a.type} | {a.title}")
    return 0


def cmd_recent(a) -> int:
    log = VAULT / "log.md"
    if not log.exists():
        print("no log yet")
        return 0
    entries = [l for l in _read(log).splitlines() if l.startswith("## [")]
    for line in entries[-a.count:]:
        print(line[3:])
    return 0


def cmd_search(a) -> int:
    """Ranked grep over the vault.

    index.md is the primary retrieval path; this exists for when you know the
    term but not which page holds it.
    """
    terms = [t.lower() for t in a.query.split() if t]
    results = []
    for p in VAULT.rglob("*.md"):
        if ".obsidian" in p.parts:
            continue
        text = _read(p)
        low = text.lower()
        score = sum(low.count(t) for t in terms)
        if not all(t in low for t in terms):
            continue
        if p.stem.lower() in low[:200]:
            score += 5
        if all(t in p.stem.lower() for t in terms):
            score += 25
        m = SUMMARY_RE.search(text[:1500])
        summary = ""
        if m:
            summary = (m.group(1) if m.group(1) is not None else m.group(2) or "")
            summary = summary.replace("''", "'")
        results.append((score, p, summary))
    results.sort(key=lambda x: -x[0])
    if not results:
        print("no matches")
        return 1
    for score, p, summary in results[:a.limit]:
        rel = p.relative_to(VAULT).as_posix()
        print(f"[{score:>4}] {p.stem}")
        print(f"        {rel}")
        if summary:
            print(f"        {summary}")
    print(f"\n{len(results)} match(es); showing {min(a.limit, len(results))}")
    return 0


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the arrows and
    # dashes used in this output. Reconfigure rather than degrade the text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="wiki.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    fa = sub.add_parser("file-answer", help="save an answer as a durable wiki page")
    fa.add_argument("title")
    fa.add_argument("--body", default="")
    fa.add_argument("--body-file", default="")
    fa.add_argument("--question", default="")
    fa.add_argument("--tags", default="")
    fa.add_argument("--summary", default="",
                    help="one-line gist for index.md; derived from the body if omitted")
    fa.add_argument("--sources", default="", help="comma-separated source paths")
    fa.add_argument("--overwrite", action="store_true")
    fa.set_defaults(fn=cmd_file_answer)

    lg = sub.add_parser("log", help="append an entry to log.md")
    lg.add_argument("type", choices=["ingest", "query", "answer", "lint", "build",
                                     "sync", "note"])
    lg.add_argument("title")
    lg.add_argument("--detail", default="")
    lg.set_defaults(fn=cmd_log)

    rc = sub.add_parser("recent", help="show recent log entries")
    rc.add_argument("--count", type=int, default=10)
    rc.set_defaults(fn=cmd_recent)

    se = sub.add_parser("search", help="ranked grep over the vault")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=12)
    se.set_defaults(fn=cmd_search)

    a = ap.parse_args()
    if not VAULT.is_dir():
        print(f"error: vault not found at {VAULT}", file=sys.stderr)
        return 2
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
