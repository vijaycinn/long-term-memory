# Install * long-term memory on a peer PC

Use this checklist to install a new, empty long-term-memory system for Copilot
CLI. The repository is designed to bootstrap cleanly: the memory store, session
hooks, wiki, and `vault` MCP server are installed by the two setup scripts. The
greenfield harness tests empty, memory-only, and workspace-backed modes in a
temporary directory (`wiki\test_bootstrap.py:1-11`, `:124-259`).

## Prerequisites

You need Windows, PowerShell, Git, and Python 3.10 or later. **Do not install
packages with `pip`.** The Python code uses the standard library; `pyyaml` is
the one optional import and `wiki\check_frontmatter.py` exits cleanly when it
is absent (`wiki\check_frontmatter.py:15-19`; import inventory checked across
`*.py`). The repository's own prerequisite check states the same requirement
(`README.md:80-84`).

```powershell
python --version
git --version
```

## 1. Select the GitHub account before cloning

This private repository is under the personal `vijaycinn` account. If GitHub
CLI is active as a `_microsoft` Enterprise Managed User (EMU), cloning can say
`Repository not found` even when the repository exists. GitHub masks the
permission failure as a 404; do not debug the URL first. An EMU also cannot
fork outside the enterprise, so "just fork it" is not a workaround
(`docs\IMPLEMENTATION-PLAN-retrieval-v4.md:293`; `README.md:146-155`).

```powershell
gh auth status
gh auth switch --user vijaycinn
git clone https://github.com/vijaycinn/long-term-memory.git
Set-Location .\long-term-memory
```

If the switch fails, request collaborator access to the private repository;
do not create an external fork.

## 2. Choose paths before installing

For the simplest installation, use the defaults. The wiki is useful without a
source workspace: an empty `LTM_WIKI_WORKSPACE` creates a memory-only wiki
from the database and session store (`wiki\config.py:6-12`, `:26-40`).

The wiki resolves each path in this order: explicit script argument, environment
variable, `wiki-config.json`, then default (`wiki\config.py:14-15`, `:52-61`).
`COPILOT_HOME` chooses the configuration directory; the remaining variables
choose the named resource.

| Setting | Environment variable | Default |
|---|---|---|
| Copilot configuration home | `COPILOT_HOME` | `~\.copilot` |
| Vault directory | `LTM_WIKI_VAULT` | `~\ltm-vault` |
| Memory database | `LTM_MEMORY_DB` | `<COPILOT_HOME>\memory.db` |
| Session database | `LTM_SESSION_DB` | `<COPILOT_HOME>\session-store.db` |
| Optional source workspace | `LTM_WIKI_WORKSPACE` | empty (memory-only) |

To use a non-default location for this PowerShell session, set the variables
before running either installer. Replace the examples with directories you own;
do not copy another user's paths.

```powershell
$env:COPILOT_HOME = "$HOME\.copilot"
$env:LTM_WIKI_VAULT = "$HOME\ltm-vault"
$env:LTM_MEMORY_DB = "$env:COPILOT_HOME\memory.db"
$env:LTM_SESSION_DB = "$env:COPILOT_HOME\session-store.db"
$env:LTM_WIKI_WORKSPACE = ""
```

`install-memory.ps1` uses `COPILOT_HOME` and `LTM_MEMORY_DB` for its database
and bridge, and `install-wiki.ps1` writes the selected vault and workspace to
`<COPILOT_HOME>\wiki-config.json` (`setup\install-memory.ps1:15-17`;
`setup\install-wiki.ps1:124-143`). You can instead pass `-Vault` and
`-Workspace` to `install-wiki.ps1` (`setup\install-wiki.ps1:1-25`).

## 3. Install the memory store

Run the memory installer first.

```powershell
.\setup\install-memory.ps1
```

It creates the database, exports the context bridge, registers the two session
hooks, installs the generated hook guard, and appends the persistent-memory instruction block to
`<workspace-root>\.github\copilot-instructions.md`
(`setup\install-memory.ps1:25-36`, `:126-182`, `:183-221`, `:81-93`).

Check `<COPILOT_HOME>\settings.json`. The installer preserves other hooks,
replaces only its own entries, and creates this exact shape; replace
`<absolute-clone-path>` with the path to *your* clone if repairing it by hand.
The installer writes slash-separated absolute paths because it calls
`Path.as_posix()` (`setup\install-memory.ps1:160-174`).

```json
{
  "hooks": {
    "sessionStart": [
      {
        "type": "command",
        "bash": "python <absolute-clone-path>/hooks/ltm_session_start.py",
        "powershell": "python <absolute-clone-path>/hooks/ltm_session_start.py",
        "timeoutSec": 10000
      }
    ],
    "sessionEnd": [
      {
        "type": "command",
        "bash": "python <absolute-clone-path>/hooks/ltm_session_end.py",
        "powershell": "python <absolute-clone-path>/hooks/ltm_session_end.py",
        "timeoutSec": 10000
      }
    ]
  }
}
```

### Choose the session-start behavior

There is a deliberate configuration conflict. The memory installer registers
`ltm_session_start.py` under `sessionStart`
(`setup\install-memory.ps1:160-176`), while the configuration guidance says
to keep `sessionStart` empty to avoid startup latency
(`copilot-config\README.md:78-82`).

Choose one of these supported configurations:

1. Keep the generated `sessionStart` entry when automatic session-start
   injection matters more than startup latency.
2. Remove only the generated `ltm_session_start.py` entry from
   `<COPILOT_HOME>\settings.json` when startup latency matters more. Keep the
   `sessionEnd` entry so automatic capture remains configured.

The installer skips hook registration rather than overwriting an invalid JSON
settings file (`setup\install-memory.ps1:149-156`). Repair the JSON and rerun
the installer if that occurs.

`-SeedExampleData` is optional. The current `setup\seed_memory.py` uses the
configured memory-database path and declares that its sample data is neutral
(`setup\seed_memory.py:1-15`); leave the switch off when you want a genuinely
empty personal store. If you work from an older checkout, inspect that file
before using the switch: the prior version contained named, HR-sensitive VOF
data despite the generic option name (`docs\IMPLEMENTATION-PLAN-retrieval-v4.md:295`).

## 4. Install the wiki and MCP server

```powershell
.\setup\install-wiki.ps1
```

The installer saves the wiki configuration, creates or reuses the vault,
builds it, and registers the stdio MCP server (`setup\install-wiki.ps1:124-177`,
`:179-208`). Check `<COPILOT_HOME>\mcp-config.json` for this entry. The
installer backs up an existing file before changing it
(`setup\install-wiki.ps1:193-205`).

```json
{
  "mcpServers": {
    "vault": {
      "tools": ["*"],
      "type": "stdio",
      "command": "python",
      "args": ["<absolute-clone-path>\\wiki\\vault_mcp.py"]
    }
  }
}
```

The current generated Vault Guide uses the configured `MEMORY_MODULE` and
`BUILD_DIR` variables rather than a machine-specific path
(`wiki\build.py:20-21`, `:404-409`). If an older checkout generates a hardcoded path in
the vault, use this checklist's clone-relative commands and update the checkout
before relying on the generated guide.

`hooks\enforce_settings.py` was removed on 2026-09-02. It enforced a single
guard — keeping `settings.model` off `auto` — and was referenced only by a
`hook-guard.json` entry that was never wired into `settings.json`, so it never
ran. If an older checkout still has it, deleting it is safe; also remove its
entry from `<COPILOT_HOME>\hooks\hook-guard.json`, otherwise the guard
registers a hook pointing at a script that no longer exists.

Do not copy `copilot-config\hooks\hook-guard.json` directly. The current
installer copies it to `<COPILOT_HOME>\hooks\hook-guard.json`, substitutes
`{{REPO_ROOT}}` with your clone path, and refuses to install it if a listed
script is absent (`setup\install-memory.ps1:184-221`). The current template
registers four `sessionEnd` scripts — `prune_orphan_hooks.py`,
`prune_copilot_state.py`, `prune_mcp_oauth.ps1`, and `plugin_extensions.py`
(`copilot-config\hooks\hook-guard.json`). A 2026-09-02 fix (commit `5409c4c`)
replaced a prior broken reference to a missing script with
`prune_mcp_oauth.ps1`, shipping the script instead of only referencing it. If
using an older checkout, verify every configured command targets a file that
exists: the prior template used machine-specific paths and referenced an
absent script (`docs\IMPLEMENTATION-PLAN-retrieval-v4.md:294`).

## 5. Set your identity

Nothing prompts for identity. The bridge renders its identity section from the
single entity whose `is_self` value is set; without one, no identity section
appears (`memory.ps1:322-341`; `CAPABILITIES.md:223`).

```powershell
. .\memory.ps1
Set-Identity -Name "Your Name"
Export-MemoryContext
```

`Set-Identity` accepts optional `-EntityType`, `-Notes`, and `-Attributes`;
the command above uses its required `-Name` parameter and the default
`person` entity type (`memory.ps1:331-337`).

## 6. Optional — enable semantic search

**The everyday loop needs no manual indexing step, once this section is done.**
`Remember-This` commits the fact, validates its nested batch result, marks the
vault dirty, rebuilds the generated pages, and waits for `Sync-Vault`'s
auto-index step. With vectors configured, the updated page is also in sidecar
BM25 and sqlite-vec before the command returns. Use `Remember-This -NoIndex` or
`$env:LTM_VAULT_AUTOINDEX = '0'` for an intentional lexical-only result.
Everything below is the **one-time bootstrap** for the optional semantic layer;
manual `vault_vectors.py index` is now a maintenance/recovery command, not a
daily requirement.

Everything above gives you a working vault with **lexical** search. Semantic
search is opt-in and gated on `sqlite-vec` plus an embedding endpoint. Without
that configuration, `Remember-This` reports `index_state = unavailable`,
leaves the pages current, and succeeds with `semantic_current = false`.
`-NoIndex` reports `disabled` instead. If a configured index fails or is
partial, the fact remains committed, but `Remember-This` throws and leaves the
dirty marker for retry.

It is worth enabling. Measured on a 619-page vault
(`docs\bench\PHASE2-GATE.md`): recall@5 rose from **0.370 to 0.556**, and the
paraphrase class — queries that use different words from the page — rose from
**exactly 0.000** to 0.182. Lexical search cannot match a word that is not
present, so that class is unreachable without embeddings.

**You need:** an Azure OpenAI (or AI Services) resource with a
`text-embedding-3-small` deployment, and the Azure CLI. There is no API key to
manage — auth is a bearer token from your own `az login`.

```powershell
pip install sqlite-vec          # the ONLY pip dependency in this repo, and it is optional
az login

python -c "import sys; sys.path.insert(0,'wiki'); import config; config.save(embed_endpoint='https://<your-resource>.cognitiveservices.azure.com', embed_deployment='text-embedding-3-small', embed_dimensions='512')"

python wiki\vault_embed.py      # smoke test: prints the vector dimension
python wiki\vault_vectors.py index       # optional maintenance/recovery
python wiki\vault_vectors.py status
```

> `python wiki\vault_vectors.py index --limit-pages N` is a scoped smoke-test
> update. It preserves chunks and BM25 rows outside the selected page set,
> guarded by `test_limited_index_preserves_out_of_scope_chunks`. The sidecar is
> still excluded from git, so an external backup remains sensible for broad
> maintenance.

**Why 512 dimensions and not 1536.** `text-embedding-3-small` is a Matryoshka
model, so the vector can be truncated with little quality loss — MTEB 61.6
versus 62.3. The sidecar is three times smaller for it: ~35 MB instead of
~78 MB on a corpus this size.

**Cost.** A full build of 7,940 chunks was **$0.039**. Re-indexing is keyed on
**content hash**, not mtime, so a rebuild that changes no text re-embeds
nothing and costs **$0.00** — verified at 7,940/7,940 chunks reused. Run
`index` freely.

**The first build is slow — expect tens of minutes, not seconds.** A cold
index embeds every chunk in the vault; at today's scale (**8,285 chunks**,
verified live with `python wiki\vault_vectors.py status` while writing this
guide) that is roughly 8,300 embedding calls at approximately 650 ms of
network round-trip each (`CAPABILITIES.md:162-163` measures this round-trip
cost for a single query; the same per-call cost applies to indexing). The
observed cold-build wall-clock time is tens of minutes, reported by the vault
owner and **not independently re-timed for this guide** — running a full cold
rebuild costs real money and tens of minutes, and the live sidecar here is
already built, so re-running it just to confirm a duration was not justified.
Do not read a quiet terminal during a first index as a hang. Every
incremental `Sync-Vault` afterward re-embeds only changed chunks and finishes
in seconds, per the everyday loop above.

**Latency, honestly.** A query string seen for the first time costs ~700 ms,
almost entirely the network round-trip to embed it. A query cache in the
sidecar makes repeats ~71 ms, which is the same as the old lexical path. If you
are offline or the endpoint is unreachable, search degrades to lexical rather
than failing.

**`az login` expiry.** Tokens lapse. When they do, the session-start hook prints
a reminder telling you to re-run `az login`; until you do, search quietly falls
back to lexical. The hook reads a cached expiry rather than calling `az`, so it
adds no measurable startup cost. If semantic search is not configured at all,
the reminder never appears.

> **Do not commit the sidecar to git.** `.gitignore` excludes `*.db` on purpose.
> The BM25 index inside it stores the **full text** of every chunk — on the
> reference vault, 7.9 million characters recoverable with one SQL query,
> including customer and HR-sensitive material. It is a plaintext copy of your
> vault wearing a binary extension. Back it up outside the repository if you
> want one; rebuilding costs four cents.

## 7. Upgrade an existing database

Installers initialize a database but do not call the migration operation
(`setup\install-memory.ps1:182-200`; `core\memory_driver.py:605-641`). If you
restored or already had `memory.db`, run the idempotent migration before
continuing.

```powershell
. .\memory.ps1
Invoke-MemoryMigration
Export-MemoryContext
```

`Invoke-MemoryMigration` sends the `migrate` operation and is safe to rerun
(`memory.ps1:344-372`; `README.md:123-128`).

## 8. Verify, then restart Copilot CLI

Run the harnesses in this order from the cloned repository.

```powershell
python wiki\test_bootstrap.py
python wiki\test_vault_mcp.py
$env:PYTHONIOENCODING = "utf-8"
python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py" -t tests
```

Healthy output ends with the greenfield bootstrap success message, the MCP
test's response summary with zero problems, and `OK` from unittest discovery.
Validated from this checkout on 2026-09-03: bootstrap PASS, MCP test 9/9 OK,
and **293 tests, OK** from unittest discovery. The three commands are the
repository's standard test sequence.

Then completely restart Copilot CLI. MCP servers and hooks load at session
start, so the `vault` tools will not appear in the current session
(`setup\install-wiki.ps1:234-235`; `README.md:143-144`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Repository not found` | GitHub CLI is using an account without access; an EMU sees a masked 404. | Run `gh auth switch --user vijaycinn`, then clone again. Request collaborator access if that account cannot access the repository. |
| No identity in my context | No entity is marked as self. | Run `. .\memory.ps1`, then `Set-Identity -Name "Your Name"` and `Export-MemoryContext`. |
| `vault` tool missing from the tool list | The CLI session started before MCP registration, or MCP use is blocked by policy. | Restart Copilot CLI. If still absent, check the `mcpServers.vault` JSON above and whether your organization's Copilot CLI policy restricts custom MCP servers. |
| Hooks did not run | `settings.json` was invalid, so the installer deliberately skipped it, or the session was already running. | Repair `settings.json`, rerun `.\setup\install-memory.ps1`, and start a new CLI session. |
| Tests pass but nothing is injected | The generated start hook was removed for the startup-latency configuration, or the running session predates the hook change. | Decide whether to retain the `sessionStart` entry in step 3, then restart Copilot CLI. |
