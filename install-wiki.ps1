<#
.SYNOPSIS
    Bootstrap the LTM wiki layer on a machine that has never run it.

.DESCRIPTION
    Sets up the Karpathy-style LLM wiki over long-term memory:

      1. Resolves and saves paths (vault, optional source workspace).
      2. Creates the vault skeleton and seeds AGENTS.md — the operating
         contract that tells an agent how to use the wiki.
      3. Builds the wiki from whatever exists. With no source workspace that is
         still a complete wiki: topics, entities, learnings, sessions.
      4. Registers the MCP server so `memory_search` and the vault tools appear
         in the agent's tool list on every turn.
      5. Verifies the result.

    Idempotent — safe to re-run. Nothing is overwritten that you have edited.

.PARAMETER Vault
    Where to create the vault. Default: ~/ltm-vault

.PARAMETER Workspace
    Optional source directory of markdown/documents to compile into the wiki.
    Omit for a memory-only wiki.

.PARAMETER SkipMcp
    Do not touch mcp-config.json.

.EXAMPLE
    .\install-wiki.ps1
.EXAMPLE
    .\install-wiki.ps1 -Vault D:\notes\brain -Workspace C:\work\project
#>
[CmdletBinding()]
param(
    [string]$Vault,
    [string]$Workspace,
    [switch]$SkipMcp
)

$ErrorActionPreference = 'Stop'
$WikiDir = Join-Path $PSScriptRoot 'wiki'
$CopilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $HOME '.copilot' }

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }

Write-Host "LTM wiki bootstrap" -ForegroundColor White
Write-Host "==================" -ForegroundColor White

# --- 0. prerequisites ------------------------------------------------------
Step 0 "Checking prerequisites"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH. Install Python 3.10+ and re-run." }
$ver = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])").Trim()
Ok "python $ver ($($py.Source))"
if (-not (Test-Path $WikiDir)) { throw "wiki/ not found next to this script ($WikiDir)" }
Ok "wiki tooling at $WikiDir"

# --- 1. configure ----------------------------------------------------------
Step 1 "Resolving paths"
if (-not $Vault) { $Vault = Join-Path $HOME 'ltm-vault' }
$cfgArgs = @("vault=$Vault")
if ($Workspace) {
    if (-not (Test-Path $Workspace)) { throw "Workspace not found: $Workspace" }
    $cfgArgs += "workspace=$Workspace"
}
$saveScript = @"
import sys, json
sys.path.insert(0, r'$WikiDir')
import config
vals = dict(a.split('=', 1) for a in sys.argv[1:])
p = config.save(**vals)
print(p)
"@
$saveScript | & python - @cfgArgs | Out-Null
Ok "config written to $CopilotHome\wiki-config.json"
& python (Join-Path $WikiDir 'config.py') | ForEach-Object { Write-Host "    $_" }

# --- 2. memory database ----------------------------------------------------
Step 2 "Checking the memory database"
$memDb = Join-Path $CopilotHome 'memory.db'
if (-not (Test-Path $memDb)) {
    Warn "no memory.db yet - run .\install-memory.ps1 first for a populated wiki"
    Warn "continuing; the wiki will be created empty and fill in as memory grows"
} else {
    $facts = & python -c "import sqlite3,sys;c=sqlite3.connect(r'$memDb');print(c.execute('select count(*) from facts').fetchone()[0])"
    Ok "memory.db present ($($facts.Trim()) facts)"
}

# --- 3. vault skeleton + operating contract --------------------------------
Step 3 "Creating the vault"
New-Item -ItemType Directory -Force -Path $Vault | Out-Null
$agents = Join-Path $Vault 'AGENTS.md'
$template = Join-Path $WikiDir 'templates\AGENTS.md'
if (Test-Path $agents) {
    Ok "AGENTS.md already present - left untouched"
} elseif (Test-Path $template) {
    Copy-Item $template $agents
    Ok "seeded AGENTS.md (the operating contract - read it)"
} else {
    Warn "no AGENTS.md template found at $template"
}

# --- 4. build --------------------------------------------------------------
Step 4 "Building the wiki"
Push-Location $WikiDir
try {
    & python build.py 2>&1 | Where-Object { $_ -match '^\[' } | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }
Ok "wiki built at $Vault"

# --- 5. MCP registration ---------------------------------------------------
Step 5 "Registering the MCP server"
if ($SkipMcp) {
    Warn "skipped (-SkipMcp)"
} else {
    $mcpCfg = Join-Path $CopilotHome 'mcp-config.json'
    $regScript = @"
import json, shutil, sys
from datetime import datetime
from pathlib import Path
cfg_path = Path(r'$mcpCfg')
entry = {'tools': ['*'], 'type': 'stdio', 'command': 'python',
         'args': [r'$WikiDir\vault_mcp.py']}
cfg = {}
if cfg_path.is_file():
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
servers = cfg.setdefault('mcpServers', {})
if servers.get('vault') == entry:
    print('already registered')
else:
    if cfg_path.is_file():
        b = cfg_path.with_suffix('.json.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(cfg_path, b)
        print('backup ->', b.name)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    servers['vault'] = entry
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    print('registered vault (%d servers)' % len(servers))
"@
    $regScript | & python - | ForEach-Object { Ok $_ }
}

# --- 6. verify -------------------------------------------------------------
Step 6 "Verifying"
Push-Location $WikiDir
try {
    $env:PYTHONIOENCODING = 'utf-8'
    & python test_vault_mcp.py 2>&1 | Select-Object -Last 1 | ForEach-Object { Ok $_ }
    & python validate.py 2>&1 | Select-Object -First 4 | ForEach-Object { Write-Host "    $_" }
} finally { Pop-Location }

Write-Host "`nDone." -ForegroundColor Green
Write-Host @"

  Vault      $Vault
  Open it    Obsidian -> Open folder as vault -> $Vault
  Start at   index.md   (catalogue)      AGENTS.md (operating contract)

  Query without Obsidian:
    python "$WikiDir\wiki.py" search "term"

  Keep it current:
    python "$WikiDir\sync.py"          # incremental
    python "$WikiDir\sync.py" --full   # also rescan the workspace

  RESTART the Copilot CLI so the MCP server is loaded. You will then have
  memory_search plus the vault_* tools available on every turn.
"@ -ForegroundColor Gray
