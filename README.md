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
