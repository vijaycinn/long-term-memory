# VJ-Agency vault builder

Generates the Obsidian vault at `..\VJ-Agency` from two sources:

1. **The workspace** — every markdown file and knowledge artifact under
   `C:\workspace\agency`, classified into domains.
2. **Long-term memory** — `~\.copilot\memory.db` (topics, facts, entities,
   snapshots) and `~\.copilot\session-store.db` (sessions, checkpoints, files
   touched).

## How this reaches the agent

Nothing injects vault *content* into a session automatically. Three layers make
the agent go and get it, in increasing order of reliability — and **all three
load at session start**, so a change here takes effect in the next session, not
the current one.

| Layer | Mechanism | Reliability |
|---|---|---|
| **Instruction** | `Knowledge Vault` section in `~/.copilot/copilot-instructions.md` and the workspace `.github/copilot-instructions.md` | Always in context, but only a nudge |
| **Skill** | `skills/vault/SKILL.md`, junctioned into `~/.copilot/skills/vault` | Mandatory when invoked — but the harness surfaces only a handful of the ~180 skills per turn, chosen by relevance |
| **MCP tools** | `vault_mcp.py`, registered as `vault` in `mcp-config.json` | **Deterministic** — the tools are in the tool list on every turn |

The MCP server is the one that does not depend on retrieval luck:

| Tool | Purpose |
|---|---|
| `vault_search` | ranked lookup, returns path + summary |
| `vault_read` | full page by name or path |
| `vault_index` | catalogue overview, or one section |
| `vault_recent` | recent log entries |
| `vault_file_answer` | file an analysis back as a durable page |

```powershell
python test_vault_mcp.py   # drives the server over stdio as the harness does
```

Standard library only, ~250 lines, no dependencies. Point it at another vault
with `VAULT_PATH`.

## The LLM-wiki pattern

This vault implements [Karpathy's LLM-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
a persistent, compounding knowledge base that sits between raw sources and the
answer, so knowledge is compiled once and kept current rather than re-derived on
every question.

| Layer | Here |
|---|---|
| Raw sources (immutable) | `C:\workspace\agency\**`, `memory.db`, `session-store.db` |
| The wiki (LLM-owned) | `VJ-Agency/` |
| The schema (operating contract) | `VJ-Agency/AGENTS.md` |

Two navigation files carry the pattern:

- **`index.md`** — content-oriented catalogue, every page with a one-line gist.
  Read first to pick pages, instead of embedding-based retrieval. Regenerated
  every build from the `summary:` frontmatter each note carries.
- **`log.md`** — chronological and append-only. Fixed `## [date] type | title`
  prefix, so `grep "^## \[" log.md | tail -5` answers "what changed lately".

### The three operations

```powershell
# Ingest — sources flow in through memory, then the builder compiles them
Remember-This -Content '...' -TopicSlug '...'   # atomic facts
Sync-Vault                                       # ~6s
Sync-Vault -Full                                 # ~30s, after new workspace files

# Query — and file the good answers back so they compound
python wiki.py search "term"
python wiki.py file-answer "Title" --body-file ans.md --tags a,b
python wiki.py recent

# Lint — health of the content, not just the graph
python lint.py        # contradictions, corrections, stale, coverage, thin pages
python validate.py    # links, orphans, duplicates, frontmatter
```

`file-answer` is the operation that decides whether the vault compounds. An
analysis left in chat history is lost; filed into `10-Synthesis/` it joins the
graph, gets auto-linked to the accounts and topics it mentions, and survives
every rebuild because it carries no `generated: true`.

## Obsidian-native equivalents

Several checks are better done by Obsidian than by a script, because the native
version is live and clickable rather than a console dump. The builder emits
frontmatter (`type`, `origin`, `summary`, `modified`, `tags`, counts) precisely
so these work.

| Need | Native feature |
|---|---|
| Find a page | **Quick switcher** (`Ctrl+O`), **Search** with `path:`, `tag:`, `file:`, `line:`, `["prop":"value"]`, `/regex/` |
| Orphans | **Graph view** → Filters → *Show orphans* |
| Broken links | **Graph view** → Filters → *Show unresolved*; also the Outgoing Links pane |
| Coverage gaps | **Unlinked mentions** in the Backlinks pane — per note, exactly what `lint.py` approximates vault-wide |
| Stale, filtered lists | **Bases** views in `00-Meta/Views/` |

`.base` files are regenerated on every build by `bases.py`:

| View | Replaces |
|---|---|
| `Accounts`, `People`, `Topics` | browsing the MOCs |
| `Stale sources` | `lint.py` staleness check, but live |
| `Synthesis` | filed answers, newest first |
| `Everything` | `index.md`, sortable and groupable |

**Bases needs correctly typed frontmatter, and that is easy to break silently.**
Dates are emitted unquoted (`modified: 2026-05-20`) so Obsidian types them as
Date — quoted, they become text and every date filter returns nothing without
error. Everything else is single-quoted, because these values contain Windows
paths and a backslash inside a *double*-quoted YAML scalar starts an escape
sequence: `source: "research\graphrag.md"` is invalid YAML. Obsidian shows no
properties and every Bases view goes empty, with no message.

`check_frontmatter.py` parses all 474 blocks with a real YAML parser to make
that failure loud. Run it after any change to frontmatter emission.

## Run it

```powershell
cd C:\workspace\agency\obsidian\_build
python build.py                 # full rebuild (~30s, rescans the workspace)
python build.py --memory-only   # incremental (~6s, memory + sessions only)
python build.py --dry-run       # report what would change, touch nothing
python validate.py              # link integrity, orphans, duplicates, frontmatter
python lint.py                  # contradictions, stale claims, coverage gaps
python check_frontmatter.py     # every frontmatter block parses as real YAML
```

No dependencies beyond the Python standard library.

## Automatic sync with long-term memory

The vault is wired into the LTM write path, so it reflects what memory knows
without anyone remembering to rebuild it.

| Trigger | Effect |
|---|---|
| `Add-Fact`, `Remember-This`, `Add-Topic`, `Add-Entity`, `Add-Snapshot`, `Set-Identity` | Mark the vault stale — writes a marker only, no rebuild cost |
| `Export-MemoryContext`, `Matrix-Load` | Incremental sync |
| **sessionEnd hook** | Incremental sync, after facts/entities/checkpoints have landed |
| `$env:LTM_VAULT_AUTOSYNC = '1'` | Background rebuild on every memory write |

```powershell
. C:\workspace\agency\long-term-memory\memory.ps1
Sync-Vault           # incremental, skipped if nothing changed
Sync-Vault -Full     # also rescan the workspace
Sync-Vault -Force    # rebuild even when clean
```

Guarantees:
- **Skipped when clean** — no marker means no work.
- **Serialised** — a lock file stops a background sync colliding with a manual one.
- **Retried on failure** — a failed sync leaves the marker set, so the next run picks it up.
- **Never breaks memory** — the sessionEnd hook is best-effort; a vault problem
  cannot fail the hook or lose memory data.

Incremental sync does **not** see new or changed workspace files. Run
`Sync-Vault -Full` after adding research, docs or skills.

## Tests

```powershell
cd C:\workspace\agency\long-term-memory
python -m unittest test_vault_sync -v
```

Covers the sessionEnd hook (lock, opt-out, marker lifecycle, never-raises) and
the incremental scoping invariant below.

## What is safe

The builder deletes **only** notes whose frontmatter contains
`generated: true`, then recreates them. Any note you write by hand anywhere in
the vault survives untouched, and is surfaced under *Hand-authored notes* on
`Home` so it stays reachable in the graph.

To adopt a generated note as your own, delete its `generated: true` line.

## Files

| File | Role |
|---|---|
| `vaultlib.py` | Folder plan, classification rules, scanning, DB loading, note buffering and flush |
| `notes.py` | Note generation — entities, topics, learnings, imports, skills, sessions, work log |
| `build.py` | Entry point — calls `notes.build()`, writes MOCs, configures Obsidian |
| `navfiles.py` | `index.md` catalogue and `log.md` append-only chronology |
| `wiki.py` | Query-side CLI — `file-answer`, `log`, `recent`, `search` |
| `validate.py` | Graph checks — links, orphans, duplicate names, frontmatter |
| `lint.py` | Content checks — contradictions, corrections, staleness, coverage gaps |
| `state.json` | Cached note inventory + alias registry, so incremental runs can skip the workspace scan |
| `catalog.json` | Last scan result, for inspection |
| `last-build.json` | Last build stats |
| `.vault-dirty` | Present when memory has changed since the last sync |
| `.vault-sync.lock` | Held during a sync to serialise concurrent runs |

## Design notes

**Two rebuild modes.** A full build rescans ~950 workspace files and takes
about 30 seconds — far too slow to run every time a fact is written. The
incremental mode regenerates only notes derived from `memory.db` and the
session store, reusing the cached inventory in `state.json`, and finishes in
about 6 seconds.

**Origin stamping is what makes incremental safe.** Every generated note carries
`origin: memory | workspace | index`, and an incremental run deletes only
`origin: memory`. Inferring origin from the folder was tried first and was
wrong: `02-People/Accounts` holds both entity notes (memory-derived) and
imported account documents (workspace-derived), so a folder rule silently
deleted seven imported documents that the incremental run could not rebuild.
Notes with no `origin` are treated as workspace-owned for the same reason.

**The inventory is read back from disk**, not reconstructed from the run's own
bookkeeping. Reconstruction drifted (458 records for 471 notes), and a note
missing from the inventory has its links escaped as unresolved on the next
incremental run.

**Classification** is path-driven (`RULES` in `vaultlib.py`). Adding a folder to
the workspace usually means adding one regex there. Anything unmatched falls to
`09-Reference` rather than being silently dropped.

**Index vs import.** Vendored libraries (`anthropic/`, `.clinerules/`) and
mirrors of the skills repo are indexed by reference, not copied. Duplicating
them would triple the note count and bury the signal. Non-markdown artifacts
(HTML reports, CSVs, decks) get an index note listing them with paths, so the
wiki knows they exist.

**Link density is managed.** Related-links are computed against a registry of
entity, account, topic and skill names, then capped at 45 inbound edges per
note. Without the cap a handful of broadly-worded notes accumulate hundreds of
inbound links and the graph collapses into a hairball where everything is one
hop from everything.

**Literal `[[..]]` in source text** — common in imported docs and checkpoint
narratives — is escaped at flush time so it does not create phantom graph nodes.
Only *unresolved* targets are escaped; real links are left alone.
