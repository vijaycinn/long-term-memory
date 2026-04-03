# Long-Term Memory for Copilot CLI / VS Code

Portable, persistent memory for your agents — **no daemon required** and **portable to a new machine**.

This package gives Copilot a shared SQLite-backed memory it can read at session start and update when the user says **"remember this"**. The storage is centralized under:

- **DB**: `C:\Users\<you>\.copilot\memory.db`
- **Startup bridge**: `C:\Users\<you>\.copilot\memory-context.md`

Think of `memory.db` as the knowledge store and `memory-context.md` as the compressed "I know Kung Fu" upload your agents read when they wake up.

---

## What it does

- Stores durable facts, preferences, identities, accounts, and snapshots in SQLite
- Exports a compact markdown context file for startup injection
- Supports full-text search across memory
- Lets agents save durable knowledge on cue with `Remember-This`
- Can be copied or restored on a new machine

---

## Files

| File | Purpose |
|------|---------|
| `init-memory.sql` | Initializes the SQLite schema and FTS tables |
| `memory_driver.py` | Python backend that reads JSON ops from stdin and writes JSON results |
| `memory.ps1` | PowerShell module with helper functions such as `Add-Fact`, `Search-Memory`, and `Remember-This` |
| `install-memory.ps1` | Bootstrap script to initialize memory and wire startup instructions into agents |
| `memory_export.py` | Exports `memory.db` to a portable JSON backup |
| `memory_restore.py` | Restores a backup and regenerates `memory-context.md` |
| `memory-health.bat` | Quick smoke test for the whole system |
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

Then in any session:

```powershell
. .\memory.ps1
Matrix-Load
Remember-This -Content "My core territories are 0807, 0808, 0909, 0910, 0911"
Search-Memory -Query "territories"
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
