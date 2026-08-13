"""wiki.py — operate the vault the way the LLM-wiki pattern expects.

The generated layer is compiled from sources and is rebuilt wholesale. This CLI
covers the other half: knowledge that originates in a conversation. An analysis
you produced, a comparison you were asked for, a connection you noticed — those
are new knowledge, and without somewhere to put them they evaporate into chat
history and the vault plateaus.

Pages written here carry no `generated: true`, so the builder never touches them.

  python wiki.py file-answer "Title" --body-file ans.md --tags saif,pricing
  python wiki.py file-answer "Title" --body "..."   --question "what was asked"
  python wiki.py ingest article.md --title "..." --url https://...
  python wiki.py index             # refresh index.md only (~1s, no rebuild)
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


def cmd_index(a) -> int:
    """Refresh index.md without a rebuild.

    Hand-authored pages need no build — the builder never touches them — so
    after writing one the only stale artefact is the catalogue. Refreshing just
    that costs about a second, against seven for an incremental rebuild.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import navfiles
    n = navfiles.write_index()
    print(f"index.md refreshed - {n} pages")
    return 0


def _affected_pages(text: str, limit: int = 25) -> list[tuple[str, int, str]]:
    """Existing pages this source most likely bears on.

    This is the bookkeeping half of an ingest — the mechanical cross-referencing
    Karpathy's pattern says the LLM should not be doing by hand. Judgement about
    which of these actually need editing stays with the agent.

    Two signals, because exact name matching alone has poor recall here: a page
    called "Topic - Content Understanding Pricing" is obviously relevant to an
    article about Content Understanding costs, but the exact phrase never
    appears. So distinctive title words are matched as well, weighted lower than
    a full-name hit so precision still wins the ranking.
    """
    STOP = {"topic", "skill", "library", "artifacts", "learnings", "sessions",
            "work", "log", "moc", "index", "home", "the", "and", "for", "with",
            "from", "into", "notes", "note", "guide", "vs", "analysis", "review"}

    def tokens(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", s.lower())
                if w not in STOP}

    src_tokens = tokens(text)
    low = text.lower()
    scores = Counter()
    meta = {}

    for p in VAULT.rglob("*.md"):
        if ".obsidian" in p.parts or p.name in ("index.md", "log.md"):
            continue
        rel = p.relative_to(VAULT).as_posix()
        meta[p.stem] = rel
        bare = re.sub(r"^(Topic|Skill|Library|Artifacts) - ", "", p.stem)

        exact = 0
        for cand in {p.stem, bare}:
            c = cand.strip().lower()
            if len(c) >= 4:
                exact += len(re.findall(
                    r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", low))
        if exact:
            scores[p.stem] += exact * 10        # a named mention is strong evidence

        title_tokens = tokens(bare)
        if title_tokens:
            shared = title_tokens & src_tokens
            # Require most of the title to appear, so "Azure Monitor" does not
            # match every page containing the word "azure".
            if len(shared) >= max(2, len(title_tokens) - 1):
                scores[p.stem] += len(shared)

    return [(name, n, meta.get(name, "")) for name, n in scores.most_common(limit)]


def cmd_ingest(a) -> int:
    """Register a source and report what it touches.

    Deliberately does **not** write the analysis: an ingest is the LLM reading a
    source and deciding what changed in its understanding. What a script can do
    is the filing — create the source page, work out which existing pages the
    source mentions, append to the log, refresh the catalogue — and then hand
    back a worklist.
    """
    src = Path(a.source)
    if not src.is_file():
        print(f"error: source not found: {src}", file=sys.stderr)
        return 2
    text = _read(src)
    if not text.strip():
        print(f"error: source is empty: {src}", file=sys.stderr)
        return 2

    title = a.title or src.stem.replace("_", " ").replace("-", " ").strip()
    stem = _slug(title)
    dest_dir = VAULT / "11-Sources"
    dest_dir.mkdir(parents=True, exist_ok=True)
    page = dest_dir / f"{stem}.md"
    n = 2
    while page.exists() and not a.overwrite:
        page = dest_dir / f"{stem} ({n}).md"
        n += 1

    affected = _affected_pages(text)
    words = len(text.split())

    tags = ["source"] + [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    fm = ["---", f'title: "{title}"', 'type: "source"',
          f'ingested: {NOW:%Y-%m-%d}',
          "tags: [" + ", ".join(f'"{t}"' for t in dict.fromkeys(tags)) + "]",
          f"source_path: '{src.as_posix()}'",
          f"word_count: {words}"]
    if a.url:
        fm.append(f"url: '{a.url}'")
    fm.append(f"summary: 'Ingested source - {words} words. Summary pending: replace "
              f"this line and the body with what it changed.'")
    fm.append("---")

    body = [
        "\n".join(fm), "", "[[Home]] › [[index]]", "", f"# {title}", "",
        "> [!warning] Not yet summarised",
        "> This page was created by `wiki.py ingest`. Replace the placeholder",
        "> below with what the source actually changed — not a restatement of it.",
        "",
        "## What this changed", "",
        "_Replace: what is now known that was not before, what it confirms, and "
        "what it contradicts._", "",
        "## Contradictions", "",
        "_Replace or delete. Record both claims and their dates; do not silently "
        "overwrite an existing page._", "",
        "## Pages this source bears on", "",
    ]
    if affected:
        body.append("| Page | Mentions | Location |")
        body.append("|---|--:|---|")
        for name, cnt, rel in affected:
            body.append(f"| [[{name}]] | {cnt} | `{rel}` |")
    else:
        body.append("_No existing page matched. This may be genuinely new ground._")
    body += ["", "---", "",
             f"_Source: `{src}`_" + (f" · [link]({a.url})" if a.url else ""),
             f"_Ingested {NOW:%Y-%m-%d %H:%M UTC}. Hand-authored — the builder "
             f"never rewrites this page._", ""]

    page.write_text("\n".join(body), encoding="utf-8")
    _append_log("ingest", title,
                f"- source: `{src}` ({words} words)\n"
                f"- page: `{page.relative_to(VAULT).as_posix()}`\n"
                f"- touches {len(affected)} existing page(s)")

    if not a.no_index:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import navfiles
        navfiles.write_index()

    print(f"page    -> {page}")
    print(f"source  -> {src}  ({words} words)")
    print(f"\n{len(affected)} page(s) this source bears on"
          + (" - review and update the ones that genuinely change:" if affected else ":"))
    for name, cnt, rel in affected[:15]:
        print(f"  {cnt:>4}x  {name}")
        print(f"         {rel}")
    print("\nNext: read the source, then edit the page above — say what it CHANGED.")
    print("      Atomic facts belong in memory (Remember-This), not only here.")
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

    ix = sub.add_parser("index", help="refresh index.md without a rebuild (~1s)")
    ix.set_defaults(fn=cmd_index)

    ig = sub.add_parser("ingest", help="register a source and report what it touches")
    ig.add_argument("source", help="path to the source file")
    ig.add_argument("--title", default="")
    ig.add_argument("--url", default="", help="original URL, if any")
    ig.add_argument("--tags", default="", help="comma-separated")
    ig.add_argument("--overwrite", action="store_true")
    ig.add_argument("--no-index", action="store_true",
                    help="skip the index refresh (batch ingests: refresh once at the end)")
    ig.set_defaults(fn=cmd_ingest)

    a = ap.parse_args()
    if not VAULT.is_dir():
        print(f"error: vault not found at {VAULT}", file=sys.stderr)
        return 2
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
