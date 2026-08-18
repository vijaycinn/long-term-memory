---
name: long-term-memory
description: Read and write the user's persistent long-term memory before answering from your own recall. USE THIS FIRST for any question about a customer or account, territory, prior research, a past decision, what happened in an earlier session, a working preference, or how a system in this workspace behaves. Trigger phrases include "what do we know about X", "have we looked at X before", "what did we decide about X", "why did we do X", "when did X happen", "catch me up on X". Also use whenever the user says "remember this", "don't forget", "matrix-load", or states a durable fact or preference that should survive the session. Works in Copilot CLI and in Scout. Do NOT use for live MSX or Power BI queries, open-web research, or M365 mail and calendar.
---

# long-term-memory — use what is already known, and record what is new

A SQLite store (`~/.copilot/memory.db`) with an Obsidian vault built over it and
an MCP server serving both. It exists so established knowledge is **used, not
re-derived** — answering from your own recall when a fact is already recorded is
the specific failure this prevents.

---

## 1. Read — before you answer

**With the `vault` MCP server registered** these are tools; no file reads needed:

| Tool | Use |
|---|---|
| `memory_search` | Full-text search over every recorded fact. **Works even with no vault built.** Start here |
| `vault_search` | Ranked page lookup — returns path plus summary |
| `vault_read` | A full page by name or path |
| `vault_index` | The catalogue, whole or one section |
| `vault_recent` | What changed lately |
| `vault_file_answer` | File an analysis back as a durable page |

**Without MCP**, the same things from a shell:

```powershell
. <repo>\memory.ps1
Search-Memory -Query "content understanding cost"
```

```powershell
cd <repo>\wiki
python wiki.py search "term"      # ranked, with summaries
python wiki.py recent             # what changed lately
```

### How to read well

- **Read the index, then pick.** Do not scan the vault. The index exists so you
  open two pages instead of twenty.
- **Prefer a recorded fact over your own recall.** If memory states something,
  that is the established position.
- **Check the date.** Facts carry when they were asserted. Say so if you are
  relying on something old.
- **Never invent one.** If memory does not cover it, say memory does not cover
  it, then answer from your own reasoning and label it as such.

---

## 2. Write — when something is worth keeping

The moment is while the context is loaded: end of an engagement, a research
session, or debugging that cost real time.

```powershell
. <repo>\memory.ps1
Remember-This -Content '...' -TopicSlug 'personal-memory' -FactType preference
Export-MemoryContext          # refresh the bridge the next session reads
```

`-FactType` is one of `finding`, `constraint`, `preference`, `decision`,
`pattern`.

**What is worth recording:** something a future session would otherwise
re-derive, that is true beyond this conversation, and that you can attribute to
a source, a measurement, or a labelled inference.

**What is not:** anything ephemeral ("for this PR", "skip lint for now"),
anything the user asked you not to keep, and anything sensitive — credentials,
tokens, personal data.

### Filing an analysis rather than a fact

A comparison, an argument, or a piece of research is not a fact and does not fit
in one line. File it as a page so it survives:

```powershell
cd <repo>\wiki
python wiki.py file-answer "Title" --body-file answer.md --tags a,b
```

This writes to `10-Synthesis/` **without** `generated: true`, so it survives
every rebuild. An analysis left in chat history is lost; filed, it compounds.

---

## 3. Harness differences that change what you can promise

| | Copilot CLI | Scout |
|---|---|---|
| Read via MCP | Yes | Yes, once registered and refreshed |
| Write with `Remember-This` | Yes | Yes — Scout has a shell |
| **Automatic capture at session end** | **Yes** | **No** |
| Skills | `~/.copilot/skills/` | `$scoutHome/m-skills/` |
| Plugins | Yes | No plugin system at all |

**Scout does not fire session hooks.** The `sessionStart` / `sessionEnd` hooks
live in `~/.copilot/settings.json`, which belongs to Copilot CLI; Scout has no
equivalent surface. So in Scout nothing captures the session for you.

Two consequences, and be honest about both:

1. In Scout, treat memory as **read-first**. If the user says "remember this",
   run `Remember-This` explicitly — do not assume it will be picked up later.
2. If the user relies on automatic capture, tell them to keep working in
   Copilot CLI, or to record deliberately in Scout.

**Scout's own memory is a separate thing and stays that way.** It is capped at
500 entries with a 60-day decay, has no relational structure, and is encrypted
at rest — so it cannot be read or migrated into this system. Do not offer to
merge them. This store runs alongside Scout's, and is the one that answers
*what else touches this* and *who else has seen this*.

---

## 4. Never edit generated pages

Vault pages carrying `generated: true` in their frontmatter are rebuilt from
memory and the workspace. An edit to one is destroyed on the next sync.

To make something durable, use `Remember-This` (a fact) or `file-answer` (a
page). Both survive rebuilds by design.

---

## Setup, if it is not wired up

```powershell
.\setup\install-memory.ps1     # store, session hooks, context bridge
.\setup\install-wiki.ps1       # vault, Obsidian graph and Bases, MCP for Copilot CLI
.\setup\install-scout.ps1      # MCP and this skill, for Scout
```

Then restart the harness. In Scout: **Extensions → MCP Servers → Refresh**.

Verify:

```powershell
python wiki\test_bootstrap.py    # stands the whole thing up in a temp dir
python wiki\test_vault_mcp.py    # drives the MCP server over stdio
```
