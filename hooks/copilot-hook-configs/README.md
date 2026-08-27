# Copilot hook configs

Restorable copies of the hook configurations that live in `~/.copilot/hooks/`.
Copilot CLI merges every `*.json` in that directory; unlike `settings.json` and
`config.json`, the CLI never rewrites files here, which is why user-owned hooks
belong in this directory rather than in `settings.json`.

## `hook-guard.json`

Registers `../prune_orphan_hooks.py` at `sessionEnd`.

Restore with:

```powershell
Copy-Item hook-guard.json "$env:USERPROFILE\.copilot\hooks\hook-guard.json"
```

### Why it exists

On 2026-08-27 every turn logged:

```
Execution failed: GenericFailure, Hook processor is not configured for session id: <id>
```

The cause was an orphaned Agency hook config at `~/.copilot/hooks/agency.json`
dated 2026-05-12. It invoked `node %APPDATA%/agency/hooks/copilot/agency-hook.cjs`
against a hardcoded `127.0.0.1:7824` that no longer had a listener, and it
declared `agentStop` — an event that then failed to dispatch, twice per turn.

It survived every Agency upgrade because Agency identifies its own configs by a
loopback URL embedded in the hook **command string** (`config_nonce` →
`hook_url_nonce` → `agency_hook_url` in `client/agency/src/session_hooks/copilot_adapter.rs`).
The legacy Node form keeps its URL inside the `.cjs` file, so both
`remove_copilot_hooks_at` and `cleanup_stale_copilot_hooks_at` skipped it. The
config was invisible to its own uninstaller.

The guard closes that gap for any tool, not just Agency.

### Why `sessionEnd` and not `sessionStart`

`sessionStart` was emptied on 2026-08-27 to cut startup latency —
`ltm_session_start.py`, `start-daemon.ps1` and `mcp-oauth-prune.ps1` were all
removed from it. Pruning at shutdown clears the config before the next session
loads it and costs nothing anyone waits on. Keep `sessionStart` empty.

## Manual use

```powershell
python ..\prune_orphan_hooks.py --check   # report only
python ..\prune_orphan_hooks.py           # prune
```

Log: `~/.copilot/hook-guard.log` (silent when there is nothing to do).
Backups: `~/.copilot/memory-backups/hook-guard/`.
