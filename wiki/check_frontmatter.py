"""Parse every note's frontmatter as real YAML.

Invalid YAML in frontmatter fails silently in Obsidian — properties simply do
not appear, and every Bases view filtering on them quietly returns nothing.
This makes that failure loud. Windows paths are the usual culprit: in a
double-quoted YAML scalar a backslash starts an escape sequence.
"""
import sys
from pathlib import Path

import config

VAULT = config.paths()["vault"]

try:
    import yaml
except ImportError:
    print("pyyaml not installed; run: pip install pyyaml")
    raise SystemExit(0)

bad, checked, typed = [], 0, {"date": 0, "summary": 0}
for p in sorted(VAULT.rglob("*.md")):
    if ".obsidian" in p.parts:
        continue
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        continue
    end = text.find("\n---", 3)
    if end == -1:
        bad.append((p, "unterminated frontmatter"))
        continue
    block = text[4:end]
    checked += 1
    try:
        data = yaml.safe_load(block) or {}
    except Exception as e:
        bad.append((p, str(e).splitlines()[0][:120]))
        continue
    import datetime
    if isinstance(data.get("modified"), (datetime.date, datetime.datetime)):
        typed["date"] += 1
    if data.get("summary"):
        typed["summary"] += 1

print(f"frontmatter blocks parsed : {checked}")
print(f"  typed as Date (modified): {typed['date']}")
print(f"  carrying a summary      : {typed['summary']}")
print(f"invalid                   : {len(bad)}")
for p, err in bad[:20]:
    print(f"  {p.relative_to(VAULT).as_posix()}\n      {err}")
sys.exit(1 if bad else 0)
