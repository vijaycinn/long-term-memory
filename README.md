# Long-Term Memory for Copilot CLI, the Copilot app and Scout

Persistent, portable memory for your agents — a SQLite store, automatic session
capture, and an Obsidian vault with a live graph on top. **No daemon, no pip
install, and it moves to a new machine.**

---

## TL;DR

Three layers, deliberately separable:

| Layer | What | Where it lives |
|---|---|---|
| **Storage** | `memory.db` — topics, facts, entities, snapshots | `~/.copilot/`, **never in this repo** |
| **Capture** | `sessionStart` / `sessionEnd` hooks write to it automatically | `hooks/` |
| **Search** | An Obsidian vault built from storage, plus an MCP server over both | `wiki/` |

```powershell
git clone https://github.com/vijaycinn/long-term-memory.git
cd long-term-memory
.\setup\install-memory.ps1      # DB + hooks + instruction block
.\setup\install-wiki.ps1        # Obsidian vault + graph + MCP server
.\setup\install-scout.ps1       # same memory inside Microsoft Scout
```

**The repo carries machinery, never memory.** A new machine needs the clone *and*
a `memory.db` — restore one with `python core/memory_restore.py <backup.json>`,
or start empty and let the hooks fill it.

### Where it works

| | Copilot CLI / app | Scout |
|---|---|---|
| Read memory via MCP | Yes | Yes, after `install-scout.ps1` + refresh |
| Write with `Remember-This` | Yes | Yes — Scout has a shell |
| **Automatic session capture** | **Yes** | **No — Scout fires no session hooks** |
| Skills | `~/.copilot/skills/` | `$scoutHome/m-skills/` |

Scout is a **read-first** surface. Everything you have recorded is searchable
there; nothing new is captured for you. Keep a Copilot CLI session in play if
you want capture to stay automatic.

### Scout already has a memory — why use this one

Scout keeps its own store at `$scoutHome/m-memory.json`. It is **linear and
lossy by design**: `m-memory-settings.json` caps it at **500 memories with a
60-day decay**, so old knowledge falls off the end rather than accumulating.
There is no relational structure — no topics, no entities, no links, so nothing
answers *what else touches this* or *who else has seen this*.

It is also **encrypted at rest** (the file begins with an `MSE` magic header and
is unlocked by `m-encryption-key.enc`), so it cannot be read, migrated or
indexed by anything outside Scout — including this system.

That is the reason for the split. Scout keeps its own short-term recall;
`install-scout.ps1` gives it a second, **graph-shaped** memory it can query
through MCP: uncapped, attributed, dated, and interlinked. The two do not merge,
and this one does not try to convert the other.

### Already have an Obsidian vault?

`install-wiki.ps1` **reuses it** rather than creating a second one. It looks, in
order, at any saved `wiki-config.json` (including Scout's own copy under
`$scoutHome/copilot/`), then `$LTM_WIKI_VAULT`, then `~/ltm-vault`. It reports
what it found and how many notes are already there.

The build **merges**: only notes stamped `origin: memory` are rebuilt. Anything
hand-authored, and anything imported from a workspace, is left untouched — so
pointing this at a vault you already use is safe.

Detection is deliberately conservative and will not go hunting through your
folders for something vault-shaped. An earlier version did, and immediately
picked an unrelated vault sitting beside the real one — which would have started
writing memory notes into someone else's graph. If your vault is somewhere else,
say so explicitly:

```powershell
.\setup\install-wiki.ps1 -Vault D:\notes\my-brain
```

---

## 🤖 Agent runbook — greenfield environment

**Read this if you are an agent setting this up and the only thing available is
Copilot CLI, the Copilot app, or Scout.** Run the steps in order and stop at the
first failure; each one prints what it did.

### 0. Preconditions

```powershell
python --version      # 3.10+ required; standard library only, nothing to pip install
git --version
```

If `python` is missing, stop and tell the user — nothing here works without it.
No other dependency exists: no pip packages, no daemon, no service.

### 1. Get the machinery

```powershell
git clone https://github.com/vijaycinn/long-term-memory.git
cd long-term-memory
```

Private repo under the **`vijaycinn`** account. If `gh` is signed in as a
different account — a `_microsoft` EMU account, for instance — the clone returns
a confusing `Repository not found` rather than a permission error. Check with
`gh auth status` and switch with `gh auth switch --user vijaycinn`.

### 2. Install memory

```powershell
.\setup\install-memory.ps1
```

Creates `~/.copilot/memory.db`, **registers both session hooks** in
`~/.copilot/settings.json`, exports the context bridge, and patches the
workspace instruction file. Idempotent — safe to re-run.

It preserves every hook and setting it did not write. If `settings.json` is not
valid JSON it refuses to touch the file and says so; register the hooks by hand
rather than letting it clobber the user's configuration.

**Verify the hooks landed.** This is the step that decides whether memory
persists at all — without it the module still loads and `Remember-This` still
works, so the install *looks* correct and quietly remembers nothing:

```powershell
python -c "import json,pathlib;d=json.loads(pathlib.Path.home().joinpath('.copilot/settings.json').read_text());print([e.get('powershell') for k in ('sessionStart','sessionEnd') for e in d['hooks'][k] if 'ltm_' in str(e)])"
```

Expect two paths ending `hooks/ltm_session_start.py` and `hooks/ltm_session_end.py`.

### 3. Bring memory across, if there is any

Storage is deliberately **not** in the repo, so a clone alone gives you an empty
brain. If the user has a backup:

```powershell
python core\memory_restore.py <backup.json>
```

If they do not, skip this — the hooks start filling `memory.db` from the next
session. Take a backup once there is something worth keeping:

```powershell
python core\memory_export.py                 # restorable JSON
python core\memory_export.py --portable      # flat, for a different agent harness
```

### 4. Install the wiki, the vault and the graph

```powershell
.\setup\install-wiki.ps1
```

This writes `~/.copilot/wiki-config.json`, builds the Obsidian vault, and
registers the `vault-mcp` MCP server in `~/.copilot/mcp-config.json`.

**If the user already has an Obsidian vault, it reuses it** — checking any saved
`wiki-config.json` (including Scout's copy), then `$LTM_WIKI_VAULT`, then
`~/ltm-vault`. The build merges: only `origin: memory` notes are rebuilt, so
hand-authored pages survive. If their vault is elsewhere, pass `-Vault <path>`
rather than letting it create a second one.

**Yes — it sets up the Obsidian vault and the graph.** Every build writes:

| Written | What it gives you |
|---|---|
| `.obsidian/graph.json` | Graph view with colour groups per folder — the map is readable on first open |
| `.obsidian/app.json`, `appearance.json`, `bookmarks.json` | Working defaults, so nothing has to be configured by hand |
| `00-Meta/Views/*.base` | Six **Obsidian Bases** views: Accounts, People, Topics, Stale sources, Synthesis, Everything |
| `type`, `tags`, `origin`, `summary` on every note | What the Bases filter on — a note missing them is invisible to every saved view |
| `index.md`, `log.md` | Catalogue and append-only chronology |

Then in Obsidian: **Open folder as vault** → the path printed by the installer →
`Ctrl/Cmd+G` for the graph.

Restart the harness so the MCP server is picked up.

### 4b. If the user is on Scout

Scout has **no plugin system**, so everything is done from the filesystem:

```powershell
.\setup\install-scout.ps1
```

This resolves `$scoutHome` (first of `~/.scout`, `~/.scout-dev`,
`~/.scout-cloud`, `~/.copilot`), registers the `vault` server in
`m-mcp-servers.json`, and junctions the skill into `m-skills/`.

It **parses `m-mcp-servers.json` before writing it.** A single malformed byte
makes Scout load *zero* servers and silently wipe the user's list, so if the
file does not parse the script backs it up and stops rather than guessing.

Then hand back the one UI-only step: **Extensions → MCP Servers → Refresh**.

**Tell the user the limitation plainly.** Scout fires no session hooks — the
`sessionStart` / `sessionEnd` entries live in `~/.copilot/settings.json`, which
belongs to Copilot CLI, and Scout has no equivalent. So in Scout:

- reading memory works fully;
- `Remember-This` works, because Scout has a shell;
- **nothing captures a session automatically.** If they rely on that, they
  should keep working in Copilot CLI, or record deliberately.

Scout's own memory (`$scoutHome/m-memory.json`) is **not** a migration source:
it is capped at 500 entries with 60-day decay and is encrypted at rest. This
system runs alongside it, not instead of it.

### 5. Prove it works

```powershell
python wiki\test_bootstrap.py     # empty, memory-only and full modes, in a temp dir
python wiki\test_vault_mcp.py     # drives the MCP server over stdio
```

`test_bootstrap.py` is the honest check: it stands the whole thing up from
nothing in a temporary directory and asserts on the vault, the Bases, the
frontmatter and the MCP surface. It touches nothing real.

### 6. Report to the user

Tell them: where `memory.db` is, whether it was restored or started empty,
whether the hooks registered, the vault path, and that they must **restart the
harness** before the MCP tools appear.

### If something fails

| Symptom | Cause | Do |
|---|---|---|
| `Repository not found` on clone | `gh` is signed in as the wrong account | `gh auth switch --user vijaycinn` |
| Hooks absent from `settings.json` | File was unparseable, so the installer refused | Fix the JSON, re-run `install-memory.ps1` |
| MCP tools do not appear | Harness not restarted, or org policy blocks MCP | Restart. The policy *MCP servers in Copilot* is **off by default** on Business/Enterprise seats |
| Vault builds but has no views | `write_bases()` regressed | `python wiki\test_bootstrap.py` — it asserts on this |
| Empty vault, no error | `memory.db` is empty | Expected on a fresh machine. Restore a backup, or let the hooks fill it |

---

This package gives Copilot CLI a shared SQLite-backed memory that is automatically injected into every session via hooks. The storage is centralized under:

- **DB**: `C:\Users\<you>\.copilot\memory.db`
- **Auto-injected instructions**: `C:\Users\<you>\.copilot\copilot-instructions.md` (LTM block merged via markers)
- **Legacy bridge**: `C:\Users\<you>\.copilot\memory-context.md` (for PowerShell `Matrix-Load`)

Think of `memory.db` as the knowledge store. At every session **end**, the `sessionEnd` hook extracts new knowledge and then refreshes the LTM block in `copilot-instructions.md`. When the **next** session starts, Copilot CLI reads that file before any hooks fire — so your identity, active topics, high-priority facts, and recent patterns are available from the very first turn. The `sessionStart` hook also refreshes the block as a backup, but the critical write happens at session end to avoid a timing race with file loading.

> ### 🤖 Changing this system? → **[CAPABILITIES.md](CAPABILITIES.md)**
>
> A single reference describing the **current state** of this system: the full op
> and cmdlet surface, the data model, every defect already fixed (with root
> causes, so they are not reintroduced), the invariants that must hold, measured
> performance characteristics, and a guide to extending it safely.
>
> Read it before changing anything here.

---

## What it does

- **Auto-injects memory at session start** — the LTM block is pre-baked into `copilot-instructions.md` by the previous session's `sessionEnd` hook, so it's loaded before any hooks fire
- **Auto-captures patterns at session end** via a `sessionEnd` hook, then refreshes the LTM instructions for the next session
- Stores durable facts, preferences, identities, accounts, and snapshots in SQLite
- Merges LTM context into `~/.copilot/copilot-instructions.md` using `<!-- LTM-START -->` / `<!-- LTM-END -->` markers, preserving any other instructions in that file
- Supports full-text search across memory
- Lets agents save durable knowledge on cue with `Remember-This`
- **Absorbs facts the agent saved with the harness `store_memory` tool**, which land in a separate store the bridge cannot read (see [Two stores, one memory](#two-stores-one-memory))
- **Compiles everything into a browsable, cross-referenced wiki** you can query as an agent tool or read in Obsidian (see [The knowledge wiki](#the-knowledge-wiki))
- Can be copied or restored on a new machine

---

## The knowledge wiki

Memory answers *"what do I know about X"* one fact at a time. That is enough for
the ~40-fact context block, but it does not accumulate: nothing links a fact
about an account to the research that produced it, or to the session where the
decision was made.

The `wiki/` layer compiles memory — plus the session store, plus an optional
source workspace — into a **persistent, cross-referenced Obsidian vault**. It
follows [Karpathy's LLM-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
knowledge is compiled once and kept current, rather than re-derived on every
question.

```
Raw sources ─────► the wiki ─────► the answer
memory.db          markdown        grounded, cited,
session store      cross-linked    and filed back
your workspace     AGENTS.md
  (immutable)      (LLM-owned)
```

**You do not need Obsidian.** The vault is plain markdown, and the primary query
path is `memory_search` — FTS5 straight against `memory.db`, which works before
any vault exists. Obsidian adds the graph, backlinks and Bases views on top.

### Bootstrap

```powershell
.\setup\install-memory.ps1        # the memory store (do this first)
.\setup\install-wiki.ps1          # the wiki layer
```

`install-wiki.ps1` resolves paths, seeds `AGENTS.md`, builds the wiki from
whatever exists, registers the MCP server and verifies the result. It is
idempotent. Options:

```powershell
.\setup\install-wiki.ps1 -Vault D:\notes\brain -Workspace C:\work\project
.\setup\install-wiki.ps1 -SkipMcp
```

### Three modes, decided by what exists on disk

| Mode | Condition | Result |
|---|---|---|
| `empty` | no `memory.db` yet | Tools report the gap and exit cleanly — nothing crashes |
| `memory-only` | a database, no source workspace | A complete wiki: topics, entities, learnings, sessions, tasks |
| `full` | plus a source workspace | Workspace documents classified and imported alongside |

Paths resolve **env var → `~/.copilot/wiki-config.json` → default**, so nothing
is tied to one machine:

| Setting | Env var | Default |
|---|---|---|
| vault | `LTM_WIKI_VAULT` | `~/ltm-vault` |
| source workspace | `LTM_WIKI_WORKSPACE` | none (memory-only) |
| memory db | `LTM_MEMORY_DB` | `~/.copilot/memory.db` |
| session store | `LTM_SESSION_DB` | `~/.copilot/session-store.db` |

```powershell
python wiki\config.py       # show resolved paths and mode
```

### Querying — the harness layer

Registering the MCP server puts these in the agent's tool list on **every turn**,
which is the difference between an instruction the agent may follow and a tool it
simply uses:

| Tool | Purpose |
|---|---|
| `memory_search` | **FTS5 over every fact.** Works with no vault, no Obsidian, no build step. Ranked by blended bm25 relevance + importance, and expanded with entity co-mentions (`## related_facts`, max 5). |
| `vault_search` | ranked page lookup with summaries |
| `vault_read` | a page in full; `hops=1` also lists linked pages (titles + summaries only, 2,000-char cap) |
| `vault_index` | the catalogue, whole or one section |
| `vault_recent` | recent entries from the append-only log |
| `vault_file_answer` | file an analysis back as a durable page |

**Ranking.** `memory_search` blends normalised bm25 (weight 0.7) with
importance/5 (weight 0.3) — constants `RANK_WEIGHT` / `IMPORTANCE_WEIGHT` in
`wiki/vault_mcp.py`. SQLite's `bm25()` is **negative, more-negative-is-better**;
the candidate pool is filled ascending and the most negative value normalises to
1.0. `tests/test_vault_mcp_rank.py` pins both halves of that convention and is
mutation-tested against inverting either one.

**Graph expansion.** `vault_read(hops=1)` parses `[[wikilinks]]` and lists
neighbours; inline body links rank above `## Related` links, which are
auto-generated name matches and are dropped first when truncating. Neighbour
*bodies* are never inlined. `memory_search` separately expands through
`entity_mentions`, so a hit surfaces facts sharing an entity from other topics.

The FTS query is sanitised on failure, so paths, emails and filenames — the
things people actually search for — do not raise `fts5: syntax error`.

Same operations from the shell, no MCP required:

```powershell
cd wiki
python wiki.py search "term"                        # ranked lookup
python wiki.py ingest article.md --title "..."      # register a source (~1.5s)
python wiki.py file-answer "Title" --body-file a.md # file an answer back
python wiki.py index                                # refresh index.md only (~1s)
python wiki.py recent                               # what changed lately
```

### Cost of each operation

Measured on a 479-page vault, so the tooling can be used freely in a
conversation without stalling it:

| Operation | Cost | When |
|---|--:|---|
| `memory_search` (FTS5) | **~15 ms** | every grounding question |
| `vault_read` | ~5 ms | opening a page |
| `vault_search`, first call | ~850 ms | reads every page once |
| `vault_search`, thereafter | **~105 ms** | in-process cache, revalidated by mtime |
| MCP process start | ~300 ms | once per session, not per call |
| `sync.py --mark` | ~310 ms | what `Add-Fact` pays |
| `wiki.py ingest` | ~1.5 s | includes the index refresh |
| `wiki.py index` | ~1 s | after writing a page by hand |
| `Sync-Vault` (incremental) | ~7 s | after writing to *memory* |
| `Sync-Vault -Full` | ~10 s | after adding workspace documents |

The distinction that matters: **writing a page needs no rebuild.** Hand-authored
pages carry no `generated: true`, so the builder never touches them and only the
catalogue is stale — one second, not seven. A rebuild is only required when
*memory* changed.

### Ingest — the conversational half

The build pipeline compiles sources it can see. `ingest` covers the other case:
a source arriving mid-conversation, where the value is in what it *changes*
rather than what it says.

```powershell
python wiki.py ingest article.md --title "..." --url https://...
```

It does the bookkeeping — creates a page under `11-Sources/`, cross-references
which existing pages the source bears on, logs it, refreshes the index — and
returns a worklist. Reading the source, deciding what actually changed, and
updating those pages stays with the agent, which is the only part a script
cannot do.

Cross-referencing uses two signals: a full page-name mention (weighted heavily)
plus distinctive-title-word overlap. Name matching alone had poor recall — an
article about Content Understanding costs obviously bears on
`Topic - Content Understanding Pricing`, but that exact phrase never appears in
it.

### Two navigation files

- **`index.md`** — content catalogue, every page with a one-line gist. Read
  first to choose pages; this replaces embedding-based retrieval at this scale.
- **`log.md`** — chronological, append-only, fixed `## [date] type | title`
  prefix so `grep "^## \[" log.md | tail -5` answers "what changed lately".

### Linting — health of the content, not just the graph

```powershell
python validate.py           # links, orphans, duplicate names, frontmatter
python lint.py               # contradictions, corrections, staleness, coverage gaps
python check_frontmatter.py  # every frontmatter block parses as real YAML
```

`lint.py` reads memory directly and reports:

| Check | Signal |
|---|---|
| **Contradictions** | Two facts on a topic overlap heavily and one explicitly announces a correction |
| **Corrections** | Facts saying they overturn something — verify the claim they replace is marked `superseded`, not still active |
| **Stale** | Pages whose source has not moved in 120 days |
| **Coverage** | Concepts referenced often with no page of their own |
| **Thin** / **Unfiled** | Pages carrying almost nothing; topics with load-bearing facts but no synthesis |

Findings are reported, never auto-fixed — resolving a contradiction is a
judgement about which claim is right.

Obsidian does some of this natively and better, because the result is live and
clickable: **Graph view** filters show orphans and unresolved links, the
**Backlinks pane** shows *unlinked mentions*, and the generated **Bases** views
in `00-Meta/Views/` are sortable tables over frontmatter.

### Staying in sync

Memory writes mark the wiki stale automatically — `Add-Fact`, `Remember-This`,
`Add-Topic`, `Add-Entity`, `Add-Snapshot` and `Set-Identity` drop a marker
(instant); the rebuild happens at a natural boundary.

```powershell
Sync-Vault           # incremental — memory + sessions
Sync-Vault -Full     # also rescan the source workspace
Sync-Vault -Status   # what would happen, changes nothing
```

Rebuilds also run on `Export-MemoryContext`, `Matrix-Load`, and at session end.
Set `$env:LTM_VAULT_AUTOSYNC = '1'` for a background rebuild on every write.

Guarantees: skipped when clean · serialised by a lock file · **a failed sync
leaves the marker set so the next run retries** · the sessionEnd hook is
best-effort and can never break the hook or lose memory data.

> Incremental sync does **not** see new or changed workspace files — it rebuilds
> from memory and the session store only. Run `Sync-Vault -Full` after adding
> documents.

### What you may edit

Every generated page carries `generated: true` plus an `origin`. The builder
deletes and recreates exactly those; **anything you write by hand survives**, and
is surfaced under *Hand-authored notes* on the vault's `Home`.

| Kind of knowledge | Where it belongs |
|---|---|
| An atomic fact | memory — `Remember-This`, then `Sync-Vault` |
| An argument, comparison or analysis | a page — `wiki.py file-answer` |

Filing answers back is what makes the wiki compound instead of plateau. An
analysis left in chat history is lost.

`wiki/templates/AGENTS.md` is the operating contract seeded into every new vault.
It tells an agent to read the index first, ground answers in existing pages, flag
contradictions rather than silently resolving them, and never edit generated
pages.

---

## Two stores, one memory

Copilot exposes its own `store_memory` tool. It writes to the harness's memory
service — **not** to `memory.db`. So a fact saved that way is never rendered into
the bridge and is invisible at the next session start, while everything written
with `Add-Fact` is invisible to the harness. Two stores drift apart in silence,
and each looks perfectly healthy on its own. On this machine 112 facts had
accumulated on the wrong side of that gap before it was noticed.

Every `store_memory` call is, however, recorded locally in
`~/.copilot/session-state/<id>/events.jsonl`. `Sync-AgentMemory` replays them
into SQLite, so both paths converge on one store.

```powershell
Sync-AgentMemory -WhatIf        # preview: what would be imported
Sync-AgentMemory                # last 48h
Sync-AgentMemory -SinceHours 0  # full historical backfill (~30s over ~500 sessions)
Export-MemoryContext            # surface them in the bridge
```

It runs automatically at session end over a 48-hour window, so no action is
needed day to day. Three properties worth knowing:

- **Idempotent.** Dedup is on `(topic, content)`; re-running imports nothing new.
- **Honours retractions.** A fact later downvoted with `vote_memory` is marked
  `superseded` rather than resurrected.
- **Preserves real recency.** Imported rows carry the time the memory was
  *stored*, not the time it was imported — otherwise every imported topic looks
  freshly used and pushes live work out of the bridge.

---

## Layout

Organised by function. Everything is standard-library Python and PowerShell —
nothing is installed as a package, so cross-folder imports put the sibling
directories on `sys.path` explicitly.

```
memory.ps1            the entry point you dot-source — Add-Fact, Search-Memory,
                      Remember-This, Matrix-Load, Backup-Memory, Sync-Vault
CAPABILITIES.md       agent reference: op surface, invariants, fixed defects
setup/                everything that stands the system up
core/                 the storage engine
hooks/                session lifecycle — this is what makes capture automatic
ops/                  maintenance, reporting, continuity
skills/               the agent-facing skill, for Copilot CLI and Scout
wiki/                 the vault, the graph and the MCP server
tests/                the suite
```

### `setup/`

| File | Purpose |
|------|---------|
| `install-memory.ps1` | DB, **session-hook registration**, context bridge, instruction block. Idempotent |
| `install-wiki.ps1` | Wiki config, vault build, MCP registration, verification |
| `install-scout.ps1` | **Scout**: registers the MCP server and junctions the skill. Parses before writing, because a malformed `m-mcp-servers.json` makes Scout load zero servers |
| `init-memory.sql` | SQLite schema and FTS tables |
| `memory-health.bat` | Quick smoke test for the whole system |
| `seed_memory.py` | Example data for testing (untracked) |

### `skills/`

| File | Purpose |
|------|---------|
| `long-term-memory/SKILL.md` | Teaches an agent the read-then-write loop, and the Copilot-vs-Scout differences. Link into `~/.copilot/skills/` or `$scoutHome/m-skills/` |

### `core/`

| File | Purpose |
|------|---------|
| `memory_driver.py` | Reads JSON ops on stdin, writes JSON results. Everything else goes through it |
| `memory_export.py` | Portable JSON backup; `--portable` for a different agent harness |
| `memory_restore.py` | Restores a backup and regenerates `memory-context.md` |

### `hooks/`

| File | Purpose |
|------|---------|
| `ltm_session_start.py` | Reads `memory.db`, merges the LTM block into `copilot-instructions.md` |
| `ltm_session_end.py` | Captures the session, extracts facts, refreshes the block, syncs the vault |

### `ops/`

| File | Purpose |
|------|---------|
| `ltm_backup.py` | Verified snapshots, rotation, status, restore |
| `ltm_maintain.py` | Retention and compaction — archive, prune, dedupe, VACUUM |
| `ltm_lint.py` | DB integrity; flags stale or orphaned data |
| `ltm_wiki_export.py` | Exports topics and facts as a markdown tree |
| `ltm_bench.py` | Scale benchmark over synthetic databases |
| `retrieval_bench.py` | **Retrieval quality** — recall@5, MRR, p50/p95 over a golden query set. Freezes `lexical_v0` / `memory_v0` so a committed baseline stays reproducible as the live code changes. `--baseline OUT.json`, `--compare A.json B.json` |
| `backfill_entity_mentions.py` | Backfills fact-level `entity_mentions` from `facts.content`. `--dry-run` by default; reuses the `notes.py` alias discipline (drops aliases under 4 chars and `STOP_ALIASES`) so short names cannot flood the table |

### `wiki/`

| File | Purpose |
|------|---------|
| `config.py` | Path and mode resolution — argument → env var → `wiki-config.json` → default |
| `vaultlib.py` | Folder plan, classification, scanning, DB loading, note buffering and flush |
| `notes.py` | Note generation — entities, topics, learnings, imports, sessions, work log |
| `build.py` | Entry point — notes, maps of content, **Obsidian config and graph** |
| `navfiles.py` | `index.md` catalogue and `log.md` append-only chronology |
| `bases.py` | **Obsidian Bases** views generated from note frontmatter |
| `sync.py` | Marker, lock and throttled rebuild — what `Sync-Vault` calls |
| `wiki.py` | Query CLI — `search`, `file-answer`, `log`, `recent` |
| `vault_mcp.py` | **MCP server** — `memory_search` (FTS5) plus the vault tools |
| `lint.py` | Content health — contradictions, staleness, coverage gaps |
| `validate.py` | Graph health — links, orphans, duplicate names |
| `check_frontmatter.py` | Every frontmatter block parses as real YAML |
| `templates/AGENTS.md` | Operating contract seeded into each new vault |
| `test_bootstrap.py` | **Greenfield-setup proof** — empty, memory-only and full modes end to end |
| `test_vault_mcp.py` | Drives the MCP server over stdio as the harness does |

### `tests/`

`test_memory_driver.py`, `test_session_start.py`, `test_session_end.py`,
`test_lint.py`, `test_ltm_ops.py`, `test_wiki_export.py`, `test_vault_sync.py`.

Run everything:

```powershell
Set-Location .\long-term-memory
$env:PYTHONIOENCODING = "utf-8"
python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py" -t tests

# the wiki layer has two standalone harnesses
python wiki\test_bootstrap.py    # bootstraps into a temp dir; touches nothing real
python wiki\test_vault_mcp.py    # MCP protocol conformance
```

---

## Schema v2 — relevance & scalability fixes

Nineteen defects surfaced in real agent sessions. All are fixed, covered by tests, and
repaired in existing databases by `Invoke-MemoryMigration`.

| # | Symptom | Cause | Fix |
|---|---------|-------|-----|
| 1 | `Search-Memory -Query 'remember-this'` threw `no such column: this` | `-` and `:` are FTS5 operators, and the query was passed through verbatim | The driver runs your query as-is (so `OR` / `NOT` / `"phrase"` / `prefix*` still work) and, only if SQLite rejects it, auto-quotes bare tokens and retries. `-Raw` disables the retry. |
| 2 | New facts never appeared in the bridge until `Touch-Fact` was run by hand | `add_fact` left `last_accessed_at` NULL, and NULLs sort **last** under `ORDER BY last_accessed_at DESC` | `Add-Fact` sets it on insert; re-asserting an existing fact refreshes it. **On the live database this had hidden 274 of 298 facts.** |
| 3 | Importance-4 facts were written but never surfaced | One global `importance >= 4 ORDER BY importance DESC LIMIT 20` slice — 20 importance-5 facts starved everything else | Per-topic quota plus a reserved pool for importance-5 facts from other topics. Every limit is tunable. |
| 4 | Entities, snapshots and open work were stored but invisible | The exporter only rendered identity, topics and facts | Added **Known Entities**, **Where I Left Off** (latest snapshot per topic) and **Pending Work** sections. |
| 5 | Identity went stale and there was no obvious way to fix it | There is no `identity` table — it is the `entities` row with `is_self = 1` | New `Set-Identity`, which upserts in place and guarantees exactly one self row. |
| 6 | `Search-Memory -Query 'fy27-comp-plan'` found nothing despite the topic existing | Topics were indexed as `title + description`; the slug you use everywhere was excluded | Slug added to the FTS index and `fts_resolved` view (**schema v2**). Existing databases are reindexed by `Invoke-MemoryMigration`. |

Two robustness fixes shipped alongside:

- **Markdown injection** — a fact body containing a line like `## Pre-flight check`
  rendered as a real document heading and corrupted the structure of
  `memory-context.md`. All rendered values are now flattened to a single line.
- **`sys.stdin` rewrapped at import time** — `memory_driver.py`, `ltm_lint.py` and
  `ltm_wiki_export.py` replaced `sys.stdin` on import, which broke
  `unittest discover` with `ValueError: I/O operation on closed file`. It now
  happens only under `__main__`.

### Upgrading an existing database

```powershell
. .\memory.ps1
Invoke-MemoryMigration      # backfills recency, reindexes FTS to v2. Safe to re-run.
Export-MemoryContext
```

### Tuning the bridge

```powershell
Export-MemoryContext -TopicLimit 6 -PerTopicFacts 6 -MaxFacts 45
```

| Parameter | Default | Controls |
|-----------|---------|----------|
| `-TopicLimit` | 5 | How many active topics load |
| `-PerTopicFacts` | 6 | Fact quota **per topic** — this is what guarantees fairness |
| `-MaxFacts` | 40 | Hard ceiling on total facts |
| `-MinImportance` | 4 | Minimum importance to qualify |
| `-EntityLimit` | 15 | Entities listed |
| `-PendingLimit` | 10 | `todo` / `action` / `question` facts listed |

---

## Is it scalable? — measured, not assumed

`ltm_bench.py` builds synthetic databases and times the operations that actually
run in a session. Numbers from this machine, median of 5 runs:

```powershell
python ops\ltm_bench.py --sizes 1000,10000,50000,100000
```

| facts | topics | DB | `export_context` (session start) | search (selective) | search (worst case) | `add_fact` |
|------:|-------:|---:|---:|---:|---:|---:|
| 1 000 | 25 | 1.4 MB | 43 ms | 5 ms | 6 ms | 33 ms |
| 10 000 | 100 | 11.6 MB | 48 ms | 5 ms | 16 ms | 37 ms |
| 50 000 | 500 | 58 MB | 103 ms | 7 ms | 51 ms | 39 ms |
| 100 000 | 1 000 | 116 MB | **161 ms** | **7 ms** | 104 ms | 33 ms |

**Verdict: yes — after the v3 fixes.** At 100 000 facts (roughly 335× a
real-world store of ~300) session start is 161 ms and writes are flat.

It was **not** scalable before. The v3 work changed the shape of the curve:

| @ 50 000 facts | before | after | gain |
|---|---:|---:|---:|
| `export_context` | **4 341 ms** | **103 ms** | **42×** |
| `export_context` p95 | 4 493 ms | 142 ms | 32× |
| search | 405 ms | 51 ms | 8× |
| search (selective) | 180 ms | 7 ms | 26× |
| scaling behaviour | linear | **sub-linear** | — |

### What was actually wrong

1. **The killer: FTS churn on recency updates.** `export_context` bumps
   `last_accessed_at` on every fact it surfaces. Each `UPDATE` fired
   `fts_facts_update`, whose `DELETE FROM memory_fts WHERE row_id = ?` is a
   **full scan** — `memory_fts.row_id` is `UNINDEXED`. Forty touched facts
   against a 51 000-row index meant ~2 million scanned rows *per session start*.
   Fixed by guarding the trigger with `WHEN OLD.content IS NOT NEW.content`, so
   recency bumps no longer re-index anything.
2. **A second write per touch.** `facts_updated` bumped `updated_at` on every
   update, including pure recency bumps. Now guarded to content-bearing columns.
3. **Search resolved through a 4-way `UNION ALL` view.** Joining `memory_fts`
   against `fts_resolved` materialised every row in the database to return ten.
   Now the FTS ids are fetched first and resolved with bounded `IN (...)`
   lookups, so resolution is O(limit).
4. **No covering indexes** for the export queries — added
   `idx_facts_topic_rank`, `idx_facts_status_rank`, `idx_facts_pending`,
   `idx_entities_recent`.
5. **N individual `UPDATE`s** for recency — now one batched statement per table.

The worst-case search column uses a term present in a large fraction of rows, so
bm25 must score everything it matches; a *selective* term stays flat at ~7 ms
regardless of size, which is the realistic case.

### The real bottleneck is no longer the database

Once the queries were fixed, measuring the PowerShell layer end-to-end showed
something the in-process benchmark hides:

| Layer | Cost |
|---|---:|
| `python -c pass` (interpreter startup) | **435 ms** |
| + `import memory_driver` | 519 ms |
| `export_context` — actual database work | **39 ms** |
| `search` — actual database work | **5 ms** |

Roughly **93 % of wall-clock time is Python process startup**, not SQLite. Every
`Invoke-Memory` call spawns a fresh interpreter, so a command that chained
three ops paid that cost three times.

The `batch` op collapses several ops into one spawn. `Remember-This` — the most
frequently used command — now runs `add_topic` + `add_fact` + `export_context`
in a single interpreter:

| `Remember-This` | Latency |
|---|---:|
| Before (3 spawns) | 1 543 ms |
| After (1 spawn) | **676 ms** |
| | **2.3× faster** |

```powershell
# Any sequence of ops, one interpreter start
Invoke-Memory @{ op="batch"; ops=@(
    @{ op="add_topic"; slug="q3-review"; title="Q3 Review"; category="career" },
    @{ op="add_fact";  content="..."; topic_slug="q3-review"; importance=5 },
    @{ op="export_context" }
)}
```

`add_fact` accepts `topic_slug` directly so a batch never needs a round trip to
resolve the topic id.

---

## Keeping it healthy as topics accumulate

Speed is only half of scale — an ever-growing store also loses signal. Two
commands cover day-to-day upkeep.

### Retention and compaction

```powershell
Invoke-MemoryMaintenance            # DRY RUN — reports what it would do
Invoke-MemoryMaintenance -Apply     # execute
```

| Step | Default | Behaviour |
|------|---------|-----------|
| Archive stale topics | idle > 180 d | `status='archived'` — data is kept and stays searchable, it just stops competing for space in the bridge. **Topics with recent facts are protected.** |
| Prune retired facts | retired > 90 d | Deletes only facts already marked `superseded` / `disproven` / `resolved` **and** gone cold. Active facts are never touched. |
| De-duplicate | — | `UNIQUE(topic_id, content)` misses global facts because SQLite treats `NULL != NULL`; this catches them. Keeps the oldest. |
| Compact | — | WAL checkpoint, `VACUUM`, `ANALYZE`. |

Retire a fact instead of deleting it — `status='superseded'` keeps the history
and lets the pruner remove it later:

```sql
UPDATE facts SET status='superseded' WHERE id = 282;
```

### Business continuity

```powershell
Backup-Memory                       # verified snapshot + rotation
Get-MemoryBackupStatus              # HEALTHY / DEGRADED / AT RISK
Restore-Memory                      # dry run — shows what would be restored
Restore-Memory -Confirm             # actually restore
```

| Property | Value |
|---|---|
| Method | SQLite **online backup API** — not a file copy |
| Why | `memory.db` runs in WAL mode; a plain copy can capture a torn database whose committed transactions still live in the `-wal` sidecar |
| Verification | Every snapshot gets `PRAGMA integrity_check` + `foreign_key_check` + row counts **at creation**. A backup that fails is discarded, not silently kept |
| Automatic | The `sessionEnd` hook snapshots if the newest backup is older than 12 h |
| **RPO** | **≤ 12 h** unattended; 0 on demand |
| **RTO** | Seconds — `Restore-Memory -Confirm` |
| Retention | 14 rotating snapshots (`-Keep` to change) |
| Off-machine | `Backup-Memory -MirrorDir "$env:OneDrive\ltm-backups"` |
| Safety | Restore refuses an unverified backup, and saves the current DB to `.pre-restore-<stamp>` first |

The restore path is covered by a real disaster drill in `test_ltm_ops.py`:
the database is deleted outright, and separately has its header shredded, then
restored and checked for exact content equality and working search. A backup
that has never been restored is a hypothesis, not a guarantee.

### Suggested cadence

| When | Command |
|------|---------|
| Automatic, every session | snapshot via `sessionEnd` hook (≤ 12 h RPO) |
| Weekly | `Invoke-MemoryMaintenance` — review the dry run |
| Monthly | `Invoke-MemoryMaintenance -Apply` |
| Quarterly | `Restore-Memory` dry run to confirm recoverability; `python ltm_bench.py` to catch regressions |
| Occasionally | `python wiki\lint.py` — contradictions, superseded claims, coverage gaps |

---


## Quick Start

```powershell
git clone <your-repo-url>
Set-Location .\long-term-memory
.\setup\install-memory.ps1 -IncludeInstalledPluginAgents -IncludeMarketplaceCacheAgents
.\setup\install-wiki.ps1                      # optional: the knowledge wiki layer
```

> **Cloning a private copy with more than one GitHub account signed in.** `gh`
> uses its *active* account for git credentials, so a clone can fail with
> `Repository not found` even though the repository exists and you have access —
> the error is indistinguishable from a genuine 404. Switch first:
>
> ```powershell
> gh auth status                 # which account is active?
> gh auth switch --user <owner>  # or: gh repo clone <owner>/<repo>
> ```
>
> Inside an already-cloned copy this is invisible, because a per-repo
> `credential.helper` pins the right token.

That will:

1. create or update `C:\Users\<you>\.copilot\memory.db`
2. regenerate `memory-context.md`
3. patch your workspace instructions plus custom/plugin agent files with the shared memory block

### Hook Registration

The hooks are registered in `~/.copilot/settings.json`. If not already present, add:

```json
{
  "hooks": {
    "sessionStart": [
      {
        "command": "python C:/workspace/agency/long-term-memory/ltm_session_start.py",
        "timeout": 10000
      }
    ],
    "sessionEnd": [
      {
        "command": "python C:/workspace/agency/long-term-memory/ltm_session_end.py",
        "timeout": 10000
      }
    ]
  }
}
```

> **Adjust the path** to wherever you cloned this repo. Once registered, every new Copilot CLI session automatically loads your memory context — no manual `Matrix-Load` needed.

Then in any session:

```powershell
. .\memory.ps1
Matrix-Load
Remember-This -Content "My core territories are 0807, 0808, 0909, 0910, 0911"
Search-Memory -Query "territories"
```

---

## How auto-injection works

Copilot CLI reads user-level custom instructions from **one file**: `~/.copilot/copilot-instructions.md`.

> **Important timing detail**: The CLI reads this file *before* running `sessionStart` hooks. This means the LTM block must already be present in the file when the session starts. The system solves this by having the **`sessionEnd` hook** refresh the block after every session, so it's ready for the next one.

### Session lifecycle

1. **Session N ends** → `sessionEnd` hook runs:
   - Reads the session store for the just-ended session
   - Detects usage patterns (customer engagement, email drafting, code work, etc.)
   - Extracts facts and entities mentioned during the session
   - Writes them back to `memory.db`
   - **Refreshes the LTM block** in `copilot-instructions.md` with up-to-date memory

2. **Session N+1 starts** → CLI reads `copilot-instructions.md` (LTM is already there ✅) → `sessionStart` hook also refreshes the block as a backup

### What the LTM block contains

- 🧠 Identity (name, role, territory, attributes)
- 📂 Active topics (top 5 by last-accessed)
- ⭐ High-priority facts (importance ≥ 4, grouped by topic)
- ✅ Pending work items
- 🔄 Recent patterns (from session classification)
- 📋 Recent session context (matching current repo/directory)
- 👥 Known entities (people, accounts, products)

### Mid-session refresh

If you add facts or preferences during a session and want them in context immediately, use:

```powershell
Matrix-Load
```

---

## Make it available to all agents

Use **both** of these patterns:

1. **Workspace-wide**: add the memory block to `.github/copilot-instructions.md`
2. **Agent-specific**: add the same block to custom `.agent.md` files
3. **Plugin / marketplace agents**: patch the `.agent.md` files under `.copilot\installed-plugins\` and `.copilot\marketplace-cache\`

`setup\install-memory.ps1` automates both for you.

This is why the memory becomes shared across agents instead of feeling tied to one persona like `vijay-agent`.

> **Note:** plugin or marketplace updates can overwrite their `.agent.md` files. If that happens, just rerun `.\setup\install-memory.ps1 -IncludeInstalledPluginAgents -IncludeMarketplaceCacheAgents`.

---

## "Remember this" pattern

When a user says:

- "remember this"
- "don't forget"
- "keep this in mind for future sessions"

the agent should save the fact using:

```powershell
Remember-This -Content "Prefer terse HTML reports for territory reviews" -TopicSlug "reporting-preferences"
```

If you want to explicitly tell any agent to **pull shared memory into the current turn**, say:

```text
matrix-load
```

and the agent should run:

```powershell
Matrix-Load
```

That gives you a consistent cue across different agent personas.

For structured items you can still use the lower-level commands directly:

```powershell
Add-Entity -Name "City of Everett" -EntityType account -Notes "Voice agents discussion is slow-moving"
Add-Fact -TopicSlug "territory-insights-fy26" -Content "Prioritize Apps + AI motions in territories 0807/0808/0909/0910/0911" -Importance 5
Add-Snapshot -TopicSlug "territory-insights-fy26" -Title "Q3 update" -Summary "Apps + AI focus and follow-ups"
Export-MemoryContext
```

---

## Matrix move: copy your memory to a new machine

### Option A — copy the live DB

1. Copy: `C:\Users\<you>\.copilot\memory.db`
2. Place it on the new machine at the same path
3. Run:

```powershell
Set-Location <cloned-repo>\long-term-memory
.\setup\install-memory.ps1
```

### Option B — export and restore JSON

On the old machine:

```powershell
python .\core\memory_export.py
```

On the new machine:

```powershell
python .\core\memory_restore.py .\memory-backup-YYYYMMDD-HHMMSS.json
.\setup\install-memory.ps1
```

Result: your new agent session opens with your identity, priorities, and key facts already loaded — the **"I know Kung Fu"** moment.

---

## GitHub-friendly packaging

To upload this to a repo:

1. commit the repo files in this folder
2. **do not commit** your personal `memory.db`, `memory-context.md`, backup JSON, or private seed content
3. keep the code + bootstrap scripts in Git, and migrate the actual memory content via:
   - `memory.db`, or
   - `memory_export.py` / `memory_restore.py`

Recommended split:

- **repo** = reusable capability
- **memory.db** = your private brain

---

## Verification

```powershell
cmd /c .\memory-health.bat
. .\memory.ps1
Get-MemoryStats
```

If those succeed, the system is installed correctly.

