<#
.SYNOPSIS
    Register the long-term-memory vault MCP server and skill in Microsoft Scout.

.DESCRIPTION
    Scout has no plugin system, so everything is done from the filesystem:
    the MCP server is written into $scoutHome/m-mcp-servers.json and the skill
    is junctioned into $scoutHome/m-skills/.

    What Scout can and cannot do with this system, verified rather than assumed:

      Reading memory      YES - the vault MCP exposes memory_search over the
                          FTS index plus the vault tools. This is the whole
                          point and it works.
      Writing a fact      YES, manually - Scout has a shell, so
                          `. memory.ps1; Remember-This -Content '...'` works.
      Automatic capture   NO. The sessionStart/sessionEnd hooks live in
                          ~/.copilot/settings.json, which is Copilot CLI's.
                          Scout has no equivalent, so nothing captures a
                          session for you. Scout is a read-first surface;
                          keep a Copilot CLI session running if you want
                          capture to stay automatic.

    THE IMPORTANT SAFETY RULE: m-mcp-servers.json is parsed before it is
    written. A single malformed byte makes Scout load ZERO servers, silently
    wiping the user's entire list. If it does not parse, this script backs it
    up and stops rather than guessing.

.PARAMETER ScoutHome
    Override the auto-detected Scout home.

.PARAMETER SkipSkill
    Register the MCP server only; do not junction the skill.
#>
[CmdletBinding()]
param(
    [string]$ScoutHome,
    [switch]$SkipSkill
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VaultMcp = Join-Path $RepoRoot 'wiki\vault_mcp.py'
$SkillSrc = Join-Path $RepoRoot 'skills\long-term-memory'

function Ok   ($m) { Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Info ($m) { Write-Host "  [info] $m" }
function Die  ($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; exit 1 }

Write-Host "== Long-Term Memory -> Scout ==" -ForegroundColor Cyan

# ---------------------------------------------------------------- scout home
if (-not $ScoutHome) {
    # Same resolution order Scout itself uses.
    $ScoutHome = @('.scout', '.scout-dev', '.scout-cloud', '.copilot') |
        ForEach-Object { Join-Path $HOME $_ } |
        Where-Object { Test-Path $_ } |
        Select-Object -First 1
}
if (-not $ScoutHome) { Die "No Scout home found. Looked for ~/.scout, ~/.scout-dev, ~/.scout-cloud, ~/.copilot." }
Info "Scout home: $ScoutHome"

if (-not (Test-Path $VaultMcp)) { Die "vault_mcp.py not found at $VaultMcp" }

# Scout launches the server itself and does not necessarily inherit this
# shell's PATH, so resolve the interpreter to an absolute path.
$python = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $python) { Die "python not found on PATH." }
Info "Python: $python"

# ------------------------------------------------------------- MCP server
$mcpFile = Join-Path $ScoutHome 'm-mcp-servers.json'

$py = @"
import json, pathlib, shutil, sys, datetime

path   = pathlib.Path(r'$mcpFile')
python = r'$python'
script = r'$VaultMcp'

data = {'servers': {}}
if path.is_file():
    raw = path.read_text(encoding='utf-8')
    try:
        data = json.loads(raw) or {'servers': {}}
    except json.JSONDecodeError as exc:
        # Parse before write. Scout loads zero servers from a malformed file,
        # so overwriting one we cannot read would silently destroy every
        # server the user has.
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = path.with_suffix(f'.json.bak-{stamp}')
        shutil.copy2(path, backup)
        print(f'FAIL {path.name} is not valid JSON ({exc}).')
        print(f'FAIL backed up to {backup.name} and stopped. Fix it, then re-run.')
        sys.exit(1)

servers = data.setdefault('servers', {})
before  = len(servers)

servers['vault'] = {
    'builtin': False,
    'config': {
        'name': 'vault',
        'type': 'command',
        'command': python,
        'args': [script],
        'timeout': 120000,
    },
    'tools': ['memory_search', 'vault_search', 'vault_read',
              'vault_index', 'vault_recent', 'vault_file_answer'],
}

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2), encoding='utf-8')
print(f'OK vault server registered; {before} other server(s) preserved')
"@

$result = python -c $py
$result -split "`n" | ForEach-Object {
    if ($_ -match '^FAIL ') { Write-Host "  $_" -ForegroundColor Red }
    elseif ($_ -match '^OK ')  { Ok ($_ -replace '^OK ', '') }
    elseif ($_.Trim())         { Info $_ }
}
if ($LASTEXITCODE -ne 0) { exit 1 }

# ------------------------------------------------------------------- skill
if (-not $SkipSkill) {
    if (-not (Test-Path $SkillSrc)) {
        Info "No skill folder at $SkillSrc - skipping."
    } else {
        $skillsDir = Join-Path $ScoutHome 'm-skills'
        New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
        $link = Join-Path $skillsDir 'long-term-memory'
        if (Test-Path $link) { Remove-Item $link -Recurse -Force }
        # Junction rather than copy: Scout follows them, so `git pull` updates
        # the skill without re-running this script.
        New-Item -ItemType Junction -Path $link -Target $SkillSrc | Out-Null
        Ok "skill junctioned into m-skills/long-term-memory"
    }
}

# ----------------------------------------------------------------- verify
Write-Host ""
Info "Verifying the server starts and answers..."
$probe = Join-Path $RepoRoot 'wiki\test_vault_mcp.py'
if (Test-Path $probe) {
    Push-Location (Split-Path $probe -Parent)
    & python $probe 2>&1 | Select-Object -Last 1 | ForEach-Object { Ok $_ }
    Pop-Location
} else {
    Info "test_vault_mcp.py not found - skipped."
}

Write-Host ""
Write-Host "Done. One step is UI-only:" -ForegroundColor Cyan
Write-Host "  Scout -> Extensions -> MCP Servers -> Refresh"
Write-Host ""
Write-Host "Then ask Scout: 'what do we know about <topic>' - it should call memory_search."
Write-Host ""
Write-Host "Note: Scout does not fire session hooks, so nothing captures a session" -ForegroundColor Yellow
Write-Host "automatically there. Facts still persist if you run Remember-This yourself," -ForegroundColor Yellow
Write-Host "and capture stays automatic in Copilot CLI." -ForegroundColor Yellow
