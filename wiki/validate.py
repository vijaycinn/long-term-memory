"""Validate the generated wiki vault: links, orphans, frontmatter, duplicates."""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import config

VAULT = config.paths()["vault"]

notes = {}
for p in VAULT.rglob("*.md"):
    if ".obsidian" in p.parts:
        continue
    notes[p.stem] = p

print(f"notes on disk: {len(notes)}")

link_re = re.compile(r"\[\[([^\]|#]+)")
fence_re = re.compile(r"^\s*(```|~~~)")
outgoing = defaultdict(set)
incoming = defaultdict(set)
broken = []
no_fm = []
dupes = Counter()


def strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans.

    Obsidian does not render a wikilink inside code, so counting one as a link
    produces phantom "broken links" for any page that documents link syntax.
    """
    out, in_fence = [], False
    for ln in text.splitlines():
        if fence_re.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append(re.sub(r"`[^`]*`", " ", ln))
    return "\n".join(out)


for stem, p in notes.items():
    txt = p.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("---"):
        no_fm.append(stem)
    for m in link_re.finditer(strip_code(txt)):
        target = m.group(1).strip()
        outgoing[stem].add(target)
        if target in notes:
            incoming[target].add(stem)
        else:
            broken.append((stem, target))

# duplicate basenames (Obsidian resolves by name; dupes cause ambiguous links)
allpaths = [p for p in VAULT.rglob("*.md") if ".obsidian" not in p.parts]
for p in allpaths:
    dupes[p.stem] += 1
dup = {k: v for k, v in dupes.items() if v > 1}

orphans = [s for s in notes if not incoming.get(s) and s != "Home"]
dead_ends = [s for s in notes if not outgoing.get(s)]

print(f"total wikilinks: {sum(len(v) for v in outgoing.values())}")
print(f"resolved links : {sum(1 for s in outgoing for t in outgoing[s] if t in notes)}")
print(f"broken links   : {len(broken)}")
print(f"duplicate names: {len(dup)}")
print(f"missing frontmatter: {len(no_fm)}")
print(f"orphans (no inbound): {len(orphans)}")
print(f"dead ends (no outbound): {len(dead_ends)}")

if broken:
    print("\n-- broken link targets (top 30) --")
    for t, n in Counter(t for _, t in broken).most_common(30):
        print(f"  {n:>4}  {t}")
if dup:
    print("\n-- duplicate note names --")
    for k, v in sorted(dup.items(), key=lambda x: -x[1])[:20]:
        print(f"  {v}x {k}")
if orphans:
    print("\n-- orphans (sample 30) --")
    for o in sorted(orphans)[:30]:
        print("   ", o)
if no_fm:
    print("\n-- missing frontmatter --")
    for o in no_fm[:20]:
        print("   ", o)

# connectivity stats
deg = sorted(((len(incoming.get(s, ())), s) for s in notes), reverse=True)
print("\n-- most linked-to --")
for d, s in deg[:15]:
    print(f"  {d:>4}  {s}")

sys.exit(0)
