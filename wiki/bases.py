"""Bases views — the parts of query and lint that Obsidian can do natively.

Bases is a core plugin (Obsidian 1.9+) that renders live tables over note
frontmatter. Because every generated note here carries `type`, `origin`,
`summary`, `modified`, `tags` and counts, several checks that would otherwise
need a script become a saved view that is always current and clickable.

These files are rewritten on every build, so they track the schema.
"""
from __future__ import annotations

from vaultlib import DRY, VAULT

BASES: dict[str, str] = {}

BASES["Accounts"] = """\
filters:
  and:
    - 'note.type == "entity"'
    - 'note.entity_type == "account"'
views:
  - type: table
    name: All accounts
    order:
      - file.name
      - note.summary
      - note.doc_count
      - note.updated
    sort:
      - property: file.name
        direction: ASC
"""

BASES["People"] = """\
filters:
  and:
    - 'note.type == "entity"'
    - 'note.entity_type != "account"'
views:
  - type: table
    name: People and partners
    order:
      - file.name
      - note.entity_type
      - note.summary
    sort:
      - property: file.name
        direction: ASC
"""

BASES["Topics"] = """\
filters:
  and:
    - 'note.type == "topic"'
views:
  - type: table
    name: By weight
    order:
      - file.name
      - note.category
      - note.fact_count
      - note.summary
    sort:
      - property: note.fact_count
        direction: DESC
  - type: table
    name: By category
    groupBy:
      property: note.category
      direction: ASC
    order:
      - file.name
      - note.fact_count
      - note.summary
"""

# Replaces lint.py's stale check with a live, clickable view. Works only
# because `modified` is emitted unquoted and therefore typed as a Date.
BASES["Stale sources"] = """\
filters:
  and:
    - 'note.modified'
    - 'note.modified < today() - "120d"'
views:
  - type: table
    name: Oldest first
    order:
      - file.name
      - note.modified
      - note.domain
      - note.summary
    sort:
      - property: note.modified
        direction: ASC
"""

BASES["Synthesis"] = """\
filters:
  and:
    - 'note.type == "synthesis"'
views:
  - type: table
    name: Filed answers
    order:
      - file.name
      - note.created
      - note.question
      - note.summary
    sort:
      - property: note.created
        direction: DESC
"""

BASES["Everything"] = """\
views:
  - type: table
    name: By folder
    groupBy:
      property: file.folder
      direction: ASC
    order:
      - file.name
      - note.type
      - note.summary
    sort:
      - property: file.name
        direction: ASC
  - type: table
    name: Hand-authored only
    filters:
      and:
        - '!note.generated'
    order:
      - file.name
      - note.type
      - note.summary
  - type: table
    name: Recently rebuilt
    order:
      - file.name
      - note.origin
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
    limit: 50
"""


def write_bases() -> int:
    """Write the .base views. Returns how many were written."""
    if DRY:
        return 0
    out = VAULT / "00-Meta" / "Views"
    out.mkdir(parents=True, exist_ok=True)
    for name, body in BASES.items():
        (out / f"{name}.base").write_text(body, encoding="utf-8")
    return len(BASES)
