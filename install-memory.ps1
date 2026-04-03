param(
    [string]$WorkspaceRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$IncludeInstalledPluginAgents,
    [switch]$IncludeMarketplaceCacheAgents,
    [switch]$SeedExampleData
)

$ErrorActionPreference = "Stop"

$script:ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:MemoryModule = Join-Path $script:ScriptRoot "memory.ps1"
$script:InitSql = Join-Path $script:ScriptRoot "init-memory.sql"
$script:SeedScript = Join-Path $script:ScriptRoot "seed_memory.py"
$script:MemoryDb = Join-Path $env:USERPROFILE ".copilot\memory.db"
$script:MemoryContext = Join-Path $env:USERPROFILE ".copilot\memory-context.md"

function Ensure-Python {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python is required but was not found in PATH."
    }
}

function Ensure-MemoryDb {
    New-Item -ItemType Directory -Force -Path (Split-Path $script:MemoryDb -Parent) | Out-Null
    $py = @"
import sqlite3, pathlib
db = pathlib.Path(r'$script:MemoryDb')
sql = pathlib.Path(r'$script:InitSql').read_text(encoding='utf-8')
conn = sqlite3.connect(str(db))
conn.executescript(sql)
conn.close()
print(db)
"@
    python -c $py | Out-Null
}

function Add-MemoryBlockIfMissing {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Block
    )

    if (-not (Test-Path $Path)) {
        return $false
    }

    $content = Get-Content -Path $Path -Raw -Encoding UTF8
    if ($content -match '## PERSISTENT MEMORY' -or
        $content -match 'Shared User Memory' -or
        $content -match 'Remember-This -Content') {
        Write-Host "Already configured: $Path"
        return $false
    }

    Add-Content -Path $Path -Value "`r`n`r`n---`r`n`r`n$Block`r`n" -Encoding UTF8
    Write-Host "Patched: $Path"
    return $true
}

function Get-MemoryBlock {
@"
## PERSISTENT MEMORY (Long-Term Memory System)

At the start of EVERY session, load shared long-term memory:

1. Read the file: ``$($script:MemoryContext)``
2. Load the module: ``. $($script:MemoryModule)``
3. If the user says **"matrix-load"**, run:
   `Matrix-Load`
4. If the user says **"remember this"**, **"don't forget"**, or gives a durable fact/preference that should persist across sessions, run:
   `Remember-This -Content '...' -TopicSlug 'personal-memory' -FactType preference`
5. After material updates, refresh the bridge:
   `Export-MemoryContext`

This memory is shared through ``$($script:MemoryDb)``; it is not specific to a single agent persona.
"@
}

function Ensure-WorkspaceInstructions {
    param([Parameter(Mandatory)][string]$Root)

    $instructionsPath = Join-Path $Root ".github\copilot-instructions.md"
    $instructionsDir = Split-Path $instructionsPath -Parent
    New-Item -ItemType Directory -Force -Path $instructionsDir | Out-Null

    if (-not (Test-Path $instructionsPath)) {
        Set-Content -Path $instructionsPath -Encoding UTF8 -Value "# Copilot Custom Instructions"
    }

    Add-MemoryBlockIfMissing -Path $instructionsPath -Block (Get-MemoryBlock) | Out-Null
}

function Ensure-AgentFiles {
    param(
        [switch]$IncludeInstalledPlugins,
        [switch]$IncludeMarketplaceCache
    )

    $paths = @()
    $personalAgents = Join-Path $env:USERPROFILE ".copilot\agents"
    if (Test-Path $personalAgents) {
        $paths += Get-ChildItem -Path $personalAgents -Filter "*.agent.md" -File -Recurse | Select-Object -ExpandProperty FullName
    }

    if ($IncludeInstalledPlugins) {
        $pluginAgents = Join-Path $env:USERPROFILE ".copilot\installed-plugins"
        if (Test-Path $pluginAgents) {
            $paths += Get-ChildItem -Path $pluginAgents -Filter "*.agent.md" -File -Recurse | Select-Object -ExpandProperty FullName
        }
    }

    if ($IncludeMarketplaceCache) {
        $marketplaceAgents = Join-Path $env:USERPROFILE ".copilot\marketplace-cache"
        if (Test-Path $marketplaceAgents) {
            $paths += Get-ChildItem -Path $marketplaceAgents -Filter "*.agent.md" -File -Recurse | Select-Object -ExpandProperty FullName
        }
    }

    foreach ($path in ($paths | Sort-Object -Unique)) {
        Add-MemoryBlockIfMissing -Path $path -Block (Get-MemoryBlock) | Out-Null
    }
}

Write-Host "== Copilot Long-Term Memory Bootstrap ==" -ForegroundColor Cyan
Ensure-Python
Ensure-MemoryDb

if ($SeedExampleData) {
    if (Test-Path $script:SeedScript) {
        Write-Host "Seeding example data..." -ForegroundColor Yellow
        & python $script:SeedScript
    } else {
        Write-Warning "seed_memory.py not found. Skipping example data seed."
    }
}

. $script:MemoryModule
$ctx = Export-MemoryContext

Ensure-WorkspaceInstructions -Root $WorkspaceRoot
Ensure-AgentFiles -IncludeInstalledPlugins:$IncludeInstalledPluginAgents -IncludeMarketplaceCache:$IncludeMarketplaceCacheAgents

Write-Host ""
Write-Host "Memory ready." -ForegroundColor Green
Write-Host "DB      : $script:MemoryDb"
Write-Host "Context : $script:MemoryContext"
Write-Host "Loaded  : $($ctx.topics_loaded) topics, $($ctx.facts_loaded) facts"
Write-Host ""
Write-Host "Try:" -ForegroundColor Cyan
Write-Host "  . $script:MemoryModule"
Write-Host "  Matrix-Load"
Write-Host "  Remember-This -Content `"My key preference goes here`""
