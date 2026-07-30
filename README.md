# Long-Term Memory for Copilot CLI / VS Code

Portable, persistent memory for your agents — **no daemon required** and **portable to a new machine**.

This package gives Copilot CLI a shared SQLite-backed memory that is automatically injected into every session via hooks. The storage is centralized under:

- **DB**: `C:\Users\<you>\.copilot\memory.db`
- **Auto-injected instructions**: `C:\Users\<you>\.copilot\copilot-instructions.md` (LTM block merged via markers)
- **Legacy bridge**: `C:\Users\<you>\.copilot\memory-context.md` (for PowerShell `Matrix-Load`)

Think of `memory.db` as the knowledge store. At every session **end**, the `sessionEnd` hook extracts new knowledge and then refreshes the LTM block in `copilot-instructions.md`. When the **next** session starts, Copilot CLI reads that file before any hooks fire — so your identity, active topics, high-priority facts, and recent patterns are available from the very first turn. The `sessionStart` hook also refreshes the block as a backup, but the critical write happens at session end to avoid a timing race with file loading.

---

## What it does

- **Auto-injects memory at session start** — the LTM block is pre-baked into `copilot-instructions.md` by the previous session's `sessionEnd` hook, so it's loaded before any hooks fire
- **Auto-captures patterns at session end** via a `sessionEnd` hook, then refreshes the LTM instructions for the next session
- Stores durable facts, preferences, identities, accounts, and snapshots in SQLite
- Merges LTM context into `~/.copilot/copilot-instructions.md` using `<!-- LTM-START -->` / `<!-- LTM-END -->` markers, preserving any other instructions in that file
- Supports full-text search across memory
- Lets agents save durable knowledge on cue with `Remember-This`
- Can be copied or restored on a new machine

---

## Files

| File | Purpose |
|------|---------|
| `init-memory.sql` | Initializes the SQLite schema and FTS tables |
| `ltm_session_start.py` | **sessionStart hook** — reads `memory.db` and merges LTM context into `copilot-instructions.md` |
| `ltm_session_end.py` | **sessionEnd hook** — captures session patterns, extracts facts, and refreshes the LTM block in `copilot-instructions.md` for the next session |
| `ltm_lint.py` | Validates memory DB integrity and flags stale or orphaned data |
| `ltm_wiki_export.py` | Exports memory topics/facts as a wiki-friendly markdown tree |
| `memory_driver.py` | Python backend that reads JSON ops from stdin and writes JSON results |
| `memory.ps1` | PowerShell module with helper functions such as `Add-Fact`, `Search-Memory`, and `Remember-This` |
| `install-memory.ps1` | Bootstrap script to initialize memory and wire startup instructions into agents |
| `memory_export.py` | Exports `memory.db` to a portable JSON backup |
| `memory_restore.py` | Restores a backup and regenerates `memory-context.md` |
| `seed_memory.py` | Seeds example data for testing |
| `memory-health.bat` | Quick smoke test for the whole system |
| `test_session_start.py` | Tests for the sessionStart hook |
| `test_session_end.py` | Tests for the sessionEnd hook |
| `test_wiki_export.py` | Tests for wiki export |
| `test_lint.py` | Tests for the lint command |
| `test_memory_driver.py` | Tests for the driver: FTS sanitizing, recency, export fairness, identity, migration |
| `ltm_bench.py` | Scale benchmark — builds synthetic databases and times the hot paths |
| `ltm_maintain.py` | Retention and compaction: archive stale topics, prune retired facts, dedupe, VACUUM |
| `ltm_backup.py` | Business continuity: verified snapshots, rotation, status, restore |
| `test_ltm_ops.py` | Tests for retention and continuity, including a real destroy-and-restore drill |

Run everything:

```powershell
Set-Location .\long-term-memory
$env:PYTHONIOENCODING = "utf-8"
python -W ignore::ResourceWarning -m unittest discover -p "test_*.py"
```

---

## Schema v2 — relevance & scalability fixes

Six defects surfaced in real agent sessions. All are fixed, covered by tests, and
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
python ltm_bench.py --sizes 1000,10000,50000,100000
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

---


## Quick Start

```powershell
git clone <your-repo-url>
Set-Location .\long-term-memory
.\install-memory.ps1 -IncludeInstalledPluginAgents -IncludeMarketplaceCacheAgents
```

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

`install-memory.ps1` automates both for you.

This is why the memory becomes shared across agents instead of feeling tied to one persona like `vijay-agent`.

> **Note:** plugin or marketplace updates can overwrite their `.agent.md` files. If that happens, just rerun `.\install-memory.ps1 -IncludeInstalledPluginAgents -IncludeMarketplaceCacheAgents`.

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
.\install-memory.ps1
```

### Option B — export and restore JSON

On the old machine:

```powershell
python .\memory_export.py
```

On the new machine:

```powershell
python .\memory_restore.py .\memory-backup-YYYYMMDD-HHMMSS.json
.\install-memory.ps1
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
