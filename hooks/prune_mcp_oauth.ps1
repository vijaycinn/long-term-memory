#!/usr/bin/env pwsh
# MCP OAuth cache pruner — runs at sessionEnd (never sessionStart, to keep
# startup latency at zero) to prevent duplicate cache entries from causing
# cold-start auth thrash (the WorkIQ-Mail-MCP-Server symptom from 6/8/2026).
#
# Behavior — duplicate metadata entries only:
#   - Group .json metadata files by serverUrl.
#   - For each group with >1 entry, keep the one with the freshest non-expired access token
#     (tie-break on most recent mtime).
#   - Move losers to ~/.copilot/mcp-oauth-config/_backup_<UTC>/.
#
# IMPORTANT — does NOT touch tokens-only files (those without .json metadata).
# Many MCP servers cache only the .tokens.json with no companion metadata;
# those are NOT orphans, they're the primary cache format for that server.
#
# Always exits 0 so it never blocks a session start.

$ErrorActionPreference = 'SilentlyContinue'
$dir = Join-Path $env:USERPROFILE '.copilot\mcp-oauth-config'
if (-not (Test-Path $dir)) { exit 0 }

$logFile = Join-Path $env:USERPROFILE '.copilot\mcp-oauth-prune.log'
function Log($msg) {
    "$([DateTime]::UtcNow.ToString('o')) $msg" | Add-Content -Path $logFile -Encoding UTF8
}

# 1. Build inventory: map .json metadata to its companion .tokens.json
$entries = @{}
Get-ChildItem $dir -Filter '*.json' -File | Where-Object {
    $_.Name -notmatch '\.tokens\.json$' -and $_.Name -notmatch '\.verifier\.json$' -and $_.FullName -notmatch '_backup'
} | ForEach-Object {
    $cfg = $null
    try { $cfg = Get-Content $_.FullName -Raw | ConvertFrom-Json } catch { return }
    if (-not $cfg.serverUrl) { return }
    $tokFile = $_.FullName -replace '\.json$', '.tokens.json'
    $expAt = 0
    $hasRt = $false
    if (Test-Path $tokFile) {
        try {
            $tok = Get-Content $tokFile -Raw | ConvertFrom-Json
            $expAt = if ($tok.expiresAt) { [long]$tok.expiresAt } else { 0 }
            $hasRt = [bool]$tok.refreshToken
        } catch {}
    }
    $entries[$_.BaseName] = [PSCustomObject]@{
        BaseName     = $_.BaseName
        JsonPath     = $_.FullName
        TokensPath   = $tokFile
        ServerUrl    = $cfg.serverUrl
        ClientId     = $cfg.clientId
        RedirectUri  = $cfg.redirectUri
        IsStatic     = [bool]$cfg.isStatic
        ExpiresAt    = $expAt
        HasRefresh   = $hasRt
        Mtime        = $_.LastWriteTime
    }
}

if ($entries.Count -eq 0) { exit 0 }

$nowSec = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$backup = $null
$removedCount = 0

# 2. Per-server-URL dedupe: keep one entry, archive the rest
$groups = $entries.Values | Group-Object ServerUrl
foreach ($g in $groups) {
    if ($g.Count -le 1) { continue }
    # Prefer non-expired access tokens; tie-break on most-recent mtime
    $sorted = $g.Group | Sort-Object @{Expression = { $_.ExpiresAt -gt $nowSec }; Descending = $true},
                                     @{Expression = 'Mtime'; Descending = $true}
    $keep = $sorted[0]
    $drop = $sorted | Select-Object -Skip 1
    foreach ($d in $drop) {
        if (-not $backup) {
            $backup = Join-Path $dir ('_backup_' + [DateTime]::UtcNow.ToString('yyyyMMdd_HHmmss'))
            New-Item -ItemType Directory -Path $backup -Force | Out-Null
        }
        foreach ($p in @($d.JsonPath, $d.TokensPath)) {
            if (Test-Path $p) {
                Move-Item $p (Join-Path $backup (Split-Path $p -Leaf)) -Force
                $removedCount++
            }
        }
        Log ("pruned dup serverUrl='{0}' redirectUri='{1}' isStatic={2} expired={3}" -f $d.ServerUrl, $d.RedirectUri, $d.IsStatic, ($d.ExpiresAt -le $nowSec))
    }
    Log ("kept     serverUrl='{0}' redirectUri='{1}' isStatic={2} expired={3}" -f $keep.ServerUrl, $keep.RedirectUri, $keep.IsStatic, ($keep.ExpiresAt -le $nowSec))
}

if ($removedCount -gt 0) {
    Log ("prune complete — removed {0} files, backup={1}" -f $removedCount, $backup)
}
exit 0
