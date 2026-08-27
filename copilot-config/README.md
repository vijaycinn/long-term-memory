# Copilot CLI config snapshots

Redacted snapshots of `~/.copilot`, produced by `../ops/backup_copilot_config.py`.

```powershell
python ..\ops\backup_copilot_config.py --check   # what would change
python ..\ops\backup_copilot_config.py           # refresh snapshots
```

## Read this before restoring

Every value under a credential-bearing key is `<REDACTED>`. `mcp-config.json`
in particular will **not work** as restored — the WebIQ API key and the GitHub
`Authorization` header have to be put back by hand. The snapshot preserves the
shape of the config, which is the part that is expensive to reconstruct.

Redaction keys on the *name*, not the value, so a secret stored under an
innocuous key name would not be caught. Review the diff before committing.

`settings.json` and `config.json` are rewritten by the CLI from its own
in-memory state. These are point-in-time snapshots for reference and recovery,
not a live sync, and restoring one is a deliberate act: copy it back, then
restart the CLI. Editing them while a session is running risks being clobbered
by that session's next write.

## What is here

| File | Notes |
|---|---|
| `mcp-config.json` | user MCP servers; secrets stripped |
| `settings.json` | plugins, hooks, subagent models, footer, allowed URLs |
| `permissions-config.json` | per-directory tool approvals — expensive to rebuild by hand |
| `hooks/hook-guard.json` | sessionEnd guard + retention sweep registration |

`config.json` is deliberately absent: it is CLI-managed state (login, install
records, marketplace cache paths), not configuration worth restoring.

## `hooks/hook-guard.json` — why it exists

Copilot CLI merges every `*.json` in `~/.copilot/hooks/`. Unlike `settings.json`
and `config.json`, the CLI never rewrites files there, which is why user-owned
hooks belong in that directory.

On 2026-08-27 every turn logged:

```
Execution failed: GenericFailure, Hook processor is not configured for session id: <id>
```

The cause was an orphaned Agency hook config at `~/.copilot/hooks/agency.json`
dated 2026-05-12. It invoked `node %APPDATA%/agency/hooks/copilot/agency-hook.cjs`
against a hardcoded `127.0.0.1:7824` that no longer had a listener, and declared
`agentStop` — an event that then failed to dispatch, twice per turn.

It survived every Agency upgrade because Agency identifies its own configs by a
loopback URL embedded in the hook **command string** (`config_nonce` →
`hook_url_nonce` → `agency_hook_url` in
`client/agency/src/session_hooks/copilot_adapter.rs`). The legacy Node form keeps
its URL inside the `.cjs` file, so both `remove_copilot_hooks_at` and
`cleanup_stale_copilot_hooks_at` skipped it. The config was invisible to its own
uninstaller.

`hooks/hook-guard.json` registers two `sessionEnd` scripts that close that gap
for any tool, not just Agency:

| Script | Does |
|---|---|
| `../hooks/prune_orphan_hooks.py` | removes hook entries whose script is gone or whose loopback port has no listener |
| `../hooks/prune_copilot_state.py` | extension logs older than 7d, session-state older than 60d |

Restore with:

```powershell
Copy-Item hooks\hook-guard.json "$env:USERPROFILE\.copilot\hooks\hook-guard.json"
```

### Why `sessionEnd` and never `sessionStart`

`sessionStart` was emptied on 2026-08-27 to cut startup latency —
`ltm_session_start.py`, `start-daemon.ps1` and `mcp-oauth-prune.ps1` were all
removed from it. Doing this work at shutdown clears state before the next
session loads it and costs nothing anyone waits on. **Keep `sessionStart` empty.**

Both scripts fail open, back up before writing, and log to
`~/.copilot/hook-guard.log` (silent when there is nothing to do). Each takes
`--check` to report without changing anything.
