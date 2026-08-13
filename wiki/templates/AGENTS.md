---
title: "AGENTS"
type: "schema"
summary: "Operating contract - read index.md first, ground answers in existing pages, file good answers back, never edit generated notes."
---

# AGENTS.md — how to operate this wiki

**Read this before answering anything from this vault.** It is the operating
contract, not documentation. It exists so that knowledge already established
here is *used*, not rediscovered.

This vault follows the LLM-wiki pattern: a persistent, compounding knowledge
base that sits between raw sources and the answer. The cross-references are
already made. The synthesis already reflects what has been read. Your job is to
stand on that, extend it, and file back what you learn — not to start from
scratch every time.

---

## The three layers

| Layer | Where | Who owns it |
|---|---|---|
| **Raw sources** | the configured workspace, `memory.db`, `session-store.db` | Immutable. Read, never modify. |
| **The wiki** | this vault | The LLM. You write it. |
| **The schema** | this file | You and the user, co-evolved. |

Every generated page carries `source:` in frontmatter pointing at the file or
table it came from. **Cite the source, not the vault path.**

---

## Before you answer: the required opening move

1. **Read `index.md` first.** It catalogues every page with a one-line gist.
   Pick the handful that matter.
2. **Open those pages.** Follow `## Related` links one hop when the question
   spans topics.
3. **Check `log.md`** when the question is about recency —
   `grep "^## \[" log.md | tail -10`.
4. **Only then** reach for raw sources, and only for detail the wiki lacks.

If you answer without consulting the wiki, you are re-deriving knowledge that is
already compiled here. That is the specific failure this vault exists to prevent.

With the MCP server registered, steps 1–3 are tool calls rather than file reads:
`memory_search` (works with no vault at all), then `vault_search`, `vault_read`.

### Where to look

| Question | Start at |
|---|---|
| Who is this person or account, what do we know | `02-People/` |
| What do we know about a subject | `05-Knowledge/Topics/` |
| What has been established as durable truth | `05-Knowledge/Learnings/` |
| What happened, when | `01-Activity/Sessions/`, `01-Activity/Work Log*` |
| What did we investigate | `03-Research/` |
| How does a system work | `04-Tech/` |
| What is unfinished | `07-Tasks/Open Loops` |
| Prior analysis and filed answers | `10-Synthesis/` |

---

## Grounding rules

- **Prefer the wiki over your own recall.** If a page states something, that is
  the established position. Do not contradict it silently.
- **If you disagree with a page, say so explicitly** and flag it — see
  *Contradictions* below. Never quietly assert the opposite.
- **Importance is a signal.** Facts carry `imp 3–5`; 5 is load-bearing and has
  usually been paid for in a real mistake. Treat a 5 as a constraint.
- **Never invent a fact into a page.** Everything written here must trace to a
  source, a memory fact, or an explicitly-labelled inference.
- **Recency matters.** Check `modified:` in frontmatter; a newer page beats an
  older one on the same claim.

---

## Editing rules — what you may and may not touch

| Frontmatter | Meaning | May you edit it? |
|---|---|---|
| `generated: true` + `origin: memory` | Rebuilt from `memory.db` / session store | **No.** Edits are destroyed on the next sync. Change the source: `Add-Fact`, `Remember-This`. |
| `generated: true` + `origin: workspace` | Rebuilt from a workspace file | **No.** Edit the file named in `source:`. |
| `generated: true` + `origin: index` | A map or catalogue | **No.** Regenerated every build. |
| no `generated` line | Hand- or LLM-authored | **Yes.** Survives every rebuild. |

**To write durable knowledge, create a page without `generated: true`** — put it
in `10-Synthesis/`. To make a *fact* durable and cross-linked automatically,
write it to memory instead and let it flow into the wiki:

```powershell
. <repo>\memory.ps1
Remember-This -Content '...' -TopicSlug 'personal-memory' -FactType finding
Sync-Vault          # incremental
Sync-Vault -Full    # also rescan workspace files
```

Which to choose: **memory** for atomic facts that should surface in future
sessions and attach themselves to people and topics. **A synthesis page** for an
argument, comparison or analysis that only makes sense as a whole.

---

## Operations

### Ingest — a new source arrives

1. Read the source in full.
2. Say what changed in your understanding — do not just summarise.
3. Decide the shape: atomic facts → memory; an argument → a `10-Synthesis/` page.
4. **Attach it**: name the people, topics and prior pages it touches, as links.
5. **Flag contradictions** with existing pages. Do not silently overwrite.
6. `Sync-Vault` so generated pages pick it up.
7. `python wiki.py log ingest "<title>" --detail "..."`

### Query — answering from the wiki

1. Index → pages → Related links → raw sources only if needed.
2. Cite pages as links; cite raw claims by their `source:` path.
3. State plainly when the wiki does **not** know something. A gap named is more
   useful than a gap papered over.
4. **File good answers back.** A comparison, an analysis, a connection you found
   is new knowledge and should not evaporate into chat history:
   ```powershell
   python wiki.py file-answer "Title" --body-file answer.md --tags topic,person
   ```
   This is what makes the wiki compound rather than plateau.

### Lint — periodic health check

```powershell
python validate.py           # links, orphans, duplicate names, frontmatter
python lint.py               # contradictions, staleness, coverage gaps, thin pages
python check_frontmatter.py  # every frontmatter block parses as YAML
```

Obsidian does some of this natively and better, because the result is live and
clickable: **Graph view** filters show orphans and unresolved links, the
**Backlinks pane** shows *unlinked mentions*, and the **Bases** views in
`00-Meta/Views/` are sortable tables over frontmatter.

Report findings; do not auto-fix contradictions — they usually need a human
decision about which claim is right.

---

## Contradictions

When a new source disagrees with an existing page, **record both** rather than
picking silently:

```markdown
> [!warning] Contradiction — 2026-01-15
> Page A states X, sourced from `docs/a.md` (2025-09).
> This source states Y, sourced from `docs/b.md` (2026-01).
> Unresolved: Y is newer, but X was verified against live data.
```

Then either raise it with the user or, if memory holds the losing claim, mark
the fact `superseded` rather than deleting it — the fact that a claim was once
believed is itself information.

---

## Anti-patterns

- Answering from your own knowledge when a page already covers it.
- Scanning the vault instead of reading `index.md` or calling `vault_search`.
- Editing a `generated: true` page — the edit is destroyed on the next sync.
- Producing a good analysis and leaving it in chat instead of filing it back.
- Silently resolving a contradiction in favour of whichever page you read last.
- Treating `## Related` as a curated claim. It is auto-generated from name
  matches — a discovery aid, not an assertion of relevance.
