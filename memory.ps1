# memory.ps1 — Long-term memory PowerShell module for Copilot agents.
#
# Usage: . .\memory.ps1
#
# All functions pipe JSON to memory_driver.py (Python stdin), receive JSON stdout.
# This avoids all PowerShell/Python quoting issues — strings pass through ConvertTo-Json.

$script:MemoryDriver = Join-Path $PSScriptRoot "memory_driver.py"
$script:MemoryDb = Join-Path $env:USERPROFILE ".copilot\memory.db"
$script:MemoryContextFile = Join-Path $env:USERPROFILE ".copilot\memory-context.md"

function Invoke-Memory {
    <#
    .SYNOPSIS
        Internal dispatcher — pipes a hashtable as JSON to memory_driver.py.
    #>
    param([Parameter(Mandatory)][hashtable]$Payload)
    if (-not (Test-Path $script:MemoryDriver)) {
        throw "memory_driver.py not found at $script:MemoryDriver"
    }
    $json = $Payload | ConvertTo-Json -Compress -Depth 10
    $result = $json | python $script:MemoryDriver
    if ($LASTEXITCODE -ne 0) {
        throw "memory_driver.py exited with code ${LASTEXITCODE}: $result"
    }
    try {
        $parsed = $result | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "memory_driver.py returned invalid JSON: $result"
    }
    if ($parsed.PSObject.Properties.Name -contains 'error') {
        $etype = if ($parsed.type) { $parsed.type } else { "MemoryError" }
        throw "memory op failed [$etype]: $($parsed.error)"
    }
    return $parsed
}

function Add-Topic {
    <#
    .SYNOPSIS   Add or upsert a topic. Returns {topic_id}.
    .EXAMPLE    Add-Topic -Slug "gcc-gaps" -Title "GCC Product Gaps" -Category "technical"
    #>
    param(
        [Parameter(Mandatory)][string]$Slug,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)]
        [ValidateSet('research','customer','tool','project','career',
                     'compliance','technical','personal','analytics')]
        [string]$Category,
        [string]$Description = "",
        [ValidateSet('active','archived','resolved')][string]$Status = "active"
    )
    Invoke-Memory @{ op="add_topic"; slug=$Slug; title=$Title; category=$Category;
                     description=$Description; status=$Status }
}

function Get-Topic {
    <#
    .SYNOPSIS   Get topic metadata by slug. Returns row or {error}.
    .EXAMPLE    Get-Topic -Slug "gcc-gaps"
    #>
    param([Parameter(Mandatory)][string]$Slug)
    Invoke-Memory @{ op="get_topic"; slug=$Slug }
}

function Add-Fact {
    <#
    .SYNOPSIS   Add an atomic fact. Deduped via UNIQUE(topic_id, content).
                Returns {fact_id, inserted: bool}.
    .DESCRIPTION
                last_accessed_at is set on insert, so a new fact is immediately
                eligible for Export-MemoryContext. Calling Touch-Fact afterwards
                is no longer required (it remains available for re-ranking an
                older fact you want to resurface).
    .EXAMPLE    Add-Fact -TopicSlug "gcc-gaps" -Content "DevBox not available in GCC" -Importance 5
    #>
    param(
        [Parameter(Mandatory)][string]$Content,
        [string]$TopicSlug = "",
        [ValidateSet('insight','decision','finding','action',
                     'question','constraint','todo','preference')]
        [string]$FactType = "insight",
        [ValidateRange(1,5)][int]$Confidence = 3,
        [ValidateRange(1,5)][int]$Importance  = 3,
        [string]$Source    = "",
        [string]$SessionId = ""
    )
    $topicId = $null
    if ($TopicSlug -ne "") {
        # Upsert topic (idempotent) to resolve id
        $topicRow = Invoke-Memory @{ op="add_topic"; slug=$TopicSlug; title=$TopicSlug; category="research" }
        $topicId  = $topicRow.topic_id
    }
    Invoke-Memory @{ op="add_fact"; content=$Content; topic_id=$topicId;
                     fact_type=$FactType; confidence=$Confidence; importance=$Importance;
                     source=$Source; session_id=$SessionId }
}

function Remember-This {
    <#
    .SYNOPSIS   Save a durable preference/fact and immediately refresh memory-context.md.
    .EXAMPLE    Remember-This -Content "My core territories are 0807, 0808, 0909, 0910, 0911"
    .EXAMPLE    Remember-This -Content "Prefer terse HTML reports for territory reviews" -TopicSlug "reporting-preferences"
    #>
    param(
        [Parameter(Mandatory)][string]$Content,
        [string]$TopicSlug = "personal-memory",
        [ValidateSet('insight','decision','finding','action',
                     'question','constraint','todo','preference')]
        [string]$FactType = "preference",
        [ValidateRange(1,5)][int]$Confidence = 5,
        [ValidateRange(1,5)][int]$Importance = 4,
        [string]$Source = "Explicit user memory request"
    )

    $fact = $null
    $ctx  = $null

    # One interpreter start instead of three. Python process startup (~0.5s) is
    # far larger than the database work (~40ms), so batching is the single
    # biggest win available to this command.
    $ops = @(
        @{ op="add_topic"; slug=$TopicSlug; title=$TopicSlug; category="research" },
        @{ op="add_fact"; content=$Content; topic_slug=$TopicSlug; fact_type=$FactType;
           confidence=$Confidence; importance=$Importance; source=$Source },
        @{ op="export_context" }
    )
    $r = Invoke-Memory @{ op="batch"; ops=$ops }
    $fact = $r.results[1]
    $ctx  = $r.results[2]

    [pscustomobject]@{
        remembered   = $true
        fact_id      = $fact.fact_id
        inserted     = $fact.inserted
        topic_slug   = $TopicSlug
        context_path = $ctx.path
    }
}

function Add-Entity {
    <#
    .SYNOPSIS   Add or upsert a named entity. Returns {entity_id}.
    .EXAMPLE    Add-Entity -Name "Vijay Cinnakonda" -EntityType "person" -IsSelf
    .EXAMPLE    Add-Entity -Name "Orlando" -EntityType "person" -Notes "DSS, low engagement 2025-2026"
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]
        [ValidateSet('person','product','org','tool','account','concept','file')]
        [string]$EntityType,
        [string]$Notes      = "",
        [string]$Attributes = "",
        [switch]$IsSelf
    )
    Invoke-Memory @{ op="add_entity"; name=$Name; entity_type=$EntityType;
                     is_self=([int]$IsSelf.IsPresent); notes=$Notes; attributes=$Attributes }
}

function Add-Snapshot {
    <#
    .SYNOPSIS   Add an immutable snapshot for a topic (auto-increments seq_number per topic).
    .EXAMPLE    Add-Snapshot -TopicSlug "vof-2025" -Title "VOF Q3 FY26 Summary" -Summary "..."
    #>
    param(
        [Parameter(Mandatory)][string]$TopicSlug,
        [Parameter(Mandatory)][string]$Title,
        [string]$Summary       = "",
        [string]$Findings      = "",
        [string]$Decisions     = "",
        [string]$OpenQuestions = "",
        [string]$NextSteps     = "",
        [string]$SourceSession = ""
    )
    $topicRow = Add-Topic -Slug $TopicSlug -Title $TopicSlug -Category "research"
    Invoke-Memory @{ op="add_snapshot"; topic_id=$topicRow.topic_id; title=$Title;
                     summary=$Summary; findings=$Findings; decisions=$Decisions;
                     open_questions=$OpenQuestions; next_steps=$NextSteps;
                     source_session=$SourceSession }
}

function Touch-Fact {
    <#
    .SYNOPSIS   Update last_accessed_at for a fact (signals recent relevance).
    .EXAMPLE    Touch-Fact -FactId 42
    #>
    param([Parameter(Mandatory)][int]$FactId)
    Invoke-Memory @{ op="touch_fact"; fact_id=$FactId }
}

function Touch-Topic {
    <#
    .SYNOPSIS   Update last_accessed_at for a topic by slug.
    .EXAMPLE    Touch-Topic -Slug "gcc-gaps"
    #>
    param([Parameter(Mandatory)][string]$Slug)
    Invoke-Memory @{ op="touch_topic"; slug=$Slug }
}

function Search-Memory {
    <#
    .SYNOPSIS   Full-text search across facts, entities, topics and snapshots.
    .DESCRIPTION
                Returns a list of objects with: src, id, text, content, subtype,
                rank, sanitized.

                NOTE: the body is exposed as BOTH .text (historical) and
                .content (alias) — either works.

                Hyphens and colons are FTS5 operators. The driver runs your query
                verbatim first (so OR / NOT / "phrase" / prefix* still work) and,
                only if SQLite rejects it, auto-quotes bare tokens and retries.
                So -Query 'remember-this' now works without manual quoting;
                .sanitized is $true when the retry path was used.
                Use -Raw to disable the retry and surface the raw FTS5 error.
    .EXAMPLE    Search-Memory -Query "GCC DevBox"
    .EXAMPLE    Search-Memory -Query "remember-this"      # hyphen handled automatically
    .EXAMPLE    Search-Memory -Query 'Foundry OR Anthropic' -Limit 5
    #>
    param(
        [Parameter(Mandatory)][string]$Query,
        [int]$Limit = 10,
        [switch]$Raw
    )
    Invoke-Memory @{ op="search_memory"; query=$Query; limit=$Limit; raw=[bool]$Raw }
}

function Set-Identity {
    <#
    .SYNOPSIS   Set or correct the user's own entity — the Identity block in memory-context.md.
    .DESCRIPTION
                There is no `identity` table. The Identity section is rendered from
                the entities row where is_self = 1. This guarantees exactly one
                self row by demoting any other claimant.
    .EXAMPLE    Set-Identity -Name "Vijay Cinnakonda" -Notes "Sr DSE, Cloud & AI Apps" -Attributes '{"fiscal_year":"FY27"}'
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [ValidateSet('person','product','org','tool','account','concept','file')]
        [string]$EntityType = "person",
        [string]$Notes      = "",
        [string]$Attributes = ""
    )
    Invoke-Memory @{ op="set_identity"; name=$Name; entity_type=$EntityType;
                     notes=$Notes; attributes=$Attributes }
}

function Invoke-MemoryMigration {
    <#
    .SYNOPSIS   Idempotent upgrade + repair for databases written by older versions.
    .DESCRIPTION
                1. Backfills facts/topics whose last_accessed_at is NULL. Those rows
                   sort LAST under ORDER BY last_accessed_at DESC, so facts added
                   before the Add-Fact fix were invisible to Export-MemoryContext
                   unless Touch-Fact was run by hand.
                2. Upgrades the FTS index to schema v2 so topic slugs are searchable
                   (Search-Memory -Query 'fy27-comp-plan' used to return nothing).
                Safe to re-run. Use -ForceReindex to rebuild the index regardless.
    .EXAMPLE    Invoke-MemoryMigration
    .EXAMPLE    Invoke-MemoryMigration -ForceReindex
    #>
    param([switch]$ForceReindex)
    $r = Invoke-Memory @{ op="migrate"; force_reindex=[bool]$ForceReindex }
    Write-Host "🔧 Backfilled last_accessed_at → facts: $($r.facts_backfilled) | topics: $($r.topics_backfilled)"
    if ($r.reindexed) {
        Write-Host "🔁 FTS reindexed → $($r.reindexed.fts_rows) rows"
    }
    if ($r.optimized) {
        Write-Host "⚡ Performance schema applied (guarded triggers + covering indexes)"
    }
    if (-not $r.reindexed -and -not $r.optimized) {
        Write-Host "   Already at schema v$($r.schema_version) — nothing to do"
    } else {
        Write-Host "   Schema v$($r.from_version) → v$($r.schema_version)"
    }
    $r
}

function Matrix-Load {
    <#
    .SYNOPSIS   Load the shared long-term memory bridge plus optional focused search results.
    .EXAMPLE    Matrix-Load
    .EXAMPLE    Matrix-Load -Query "territory-preferences","City of Everett"
    .EXAMPLE    matrix-load -Refresh
    #>
    param(
        [string[]]$Query = @(),
        [switch]$Refresh
    )

    if ($Refresh -or -not (Test-Path $script:MemoryContextFile)) {
        $null = Export-MemoryContext
    }

    $stats = Invoke-Memory @{ op="get_stats" }
    $context = if (Test-Path $script:MemoryContextFile) {
        Get-Content -Path $script:MemoryContextFile -Raw -Encoding UTF8
    } else {
        ""
    }

    $searchResults = @()
    foreach ($term in $Query) {
        $hits = Search-Memory -Query $term -Limit 5
        $searchResults += [pscustomobject]@{
            query   = $term
            results = $hits
        }
    }

    [pscustomobject]@{
        db_path        = $script:MemoryDb
        context_path   = $script:MemoryContextFile
        stats          = $stats
        context        = $context
        search_results = $searchResults
    }
}

function Export-MemoryContext {
    <#
    .SYNOPSIS   Generate memory-context.md — the bridge all agents read at session start.
    .DESCRIPTION
                Facts are selected with a PER-TOPIC quota rather than one global
                slice, so no single hot topic can crowd out the others and
                importance-4 facts stay reachable as the database grows. A small
                reserved pool then admits importance-5 facts from anywhere else.
                Also emits Where-I-Left-Off (latest snapshot per topic), Pending
                Work (todo/action/question facts) and Known Entities.
                All limits are tunable.
    .EXAMPLE    Export-MemoryContext
    .EXAMPLE    Export-MemoryContext -TopicLimit 8 -PerTopicFacts 8 -MaxFacts 60
    #>
    param(
        [int]$TopicLimit    = 5,
        [int]$PerTopicFacts = 6,
        [int]$MaxFacts      = 40,
        [ValidateRange(1,5)][int]$MinImportance = 4,
        [int]$EntityLimit   = 15,
        [int]$PendingLimit  = 10
    )
    $result = Invoke-Memory @{ op="export_context"; topic_limit=$TopicLimit;
                               per_topic_facts=$PerTopicFacts; max_facts=$MaxFacts;
                               min_importance=$MinImportance; entity_limit=$EntityLimit;
                               pending_limit=$PendingLimit }
    if ($result -and $result.path) {
        Write-Host "✅ Memory context exported → $($result.path)"
        Write-Host ("   Topics: {0} | Facts: {1} | Snapshots: {2} | Pending: {3} | Entities: {4}" -f `
            $result.topics_loaded, $result.facts_loaded, $result.snapshots_loaded,
            $result.pending_loaded, $result.entities_loaded)
    }
    $result
}

function Export-Wiki {
    <#
    .SYNOPSIS   Generate browsable markdown wiki from LTM data at ~/.copilot/ltm-wiki/.
    .EXAMPLE    Export-Wiki
    #>
    $driver = Join-Path $PSScriptRoot "ltm_wiki_export.py"
    if (-not (Test-Path $driver)) {
        throw "ltm_wiki_export.py not found at $driver"
    }
    $result = '{}' | python $driver
    if ($LASTEXITCODE -ne 0) {
        throw "ltm_wiki_export.py failed: $result"
    }
    $parsed = $result | ConvertFrom-Json
    Write-Host "📚 Wiki exported → $($parsed.wiki_path)"
    Write-Host "   Topics: $($parsed.topics_exported) | Entities: $($parsed.entities_exported) | Facts: $($parsed.facts_exported)"
    $parsed
}

function Lint-Memory {
    <#
    .SYNOPSIS   Health-check the memory database. Reports stale facts, orphaned entities, empty topics, and more.
    .EXAMPLE    Lint-Memory
    #>
    $driver = Join-Path $PSScriptRoot "ltm_lint.py"
    if (-not (Test-Path $driver)) {
        throw "ltm_lint.py not found at $driver"
    }
    $result = '{}' | python $driver
    if ($LASTEXITCODE -ne 0) {
        throw "ltm_lint.py failed: $result"
    }
    $parsed = $result | ConvertFrom-Json
    if ($parsed.healthy) {
        Write-Host "✅ Memory is healthy — no issues found"
    } else {
        Write-Host "⚠️ Memory health issues found:"
        Write-Host "   $($parsed.summary)"
        Write-Host ""
        foreach ($check in $parsed.checks.PSObject.Properties) {
            $c = $check.Value
            if ($c.count -gt 0) {
                Write-Host "  🔍 $($check.Name): $($c.count) issues"
            }
        }
    }
    $parsed
}

function Get-MemoryStats {
    <#
    .SYNOPSIS   Quick status — row counts per table plus schema version.
    .EXAMPLE    Get-MemoryStats
    #>
    $stats = Invoke-Memory @{ op="get_stats" }
    $stats | Format-List
}

function Sync-AgentMemory {
    <#
    .SYNOPSIS   Import facts saved via the harness `store_memory` tool into memory.db.
    .DESCRIPTION
        The agent-facing `store_memory` tool writes to the harness's own memory
        service, which Export-MemoryContext cannot read — so those facts never
        reach the bridge and are invisible at the next session start, while
        Add-Fact writes are invisible to the harness. Every call is recorded in
        the local session event log, which this replays.

        Idempotent: dedup is on (topic, content), so re-running only refreshes
        recency. Facts later downvoted with vote_memory are marked superseded
        rather than resurrected.

        Runs automatically at session end (bounded to 48h). Use -SinceHours 0
        for a full historical backfill.
    .EXAMPLE    Sync-AgentMemory -WhatIf
    .EXAMPLE    Sync-AgentMemory                 # last 48h
    .EXAMPLE    Sync-AgentMemory -SinceHours 0   # full backfill, all sessions
    #>
    param(
        [int]$SinceHours = 48,
        [int]$Importance = 4,
        [string]$TopicSlug,
        [int]$MaxSessions,
        [switch]$WhatIf
    )
    $payload = @{ op = "sync_agent_memory"; importance = $Importance }
    if ($SinceHours -gt 0)    { $payload.since_hours  = $SinceHours }
    if ($TopicSlug)           { $payload.topic_slug   = $TopicSlug }
    if ($MaxSessions -gt 0)   { $payload.max_sessions = $MaxSessions }
    if ($WhatIf)              { $payload.dry_run      = $true }

    $r = Invoke-Memory $payload
    if ($r.error) { Write-Host "❌ $($r.error)" -ForegroundColor Red; return $r }

    if ($WhatIf) {
        Write-Host "🔍 Dry run — found $($r.calls_found) store_memory call(s), nothing written" -ForegroundColor Cyan
        if ($r.would_insert) { $r.would_insert | ForEach-Object { Write-Host "   • $_" -ForegroundColor DarkGray } }
    } else {
        Write-Host "🔗 Agent memory synced" -ForegroundColor Green
        Write-Host "   Found: $($r.calls_found) | Inserted: $($r.inserted) | Already present: $($r.already_present)" -ForegroundColor Gray
        Write-Host "   Topics created: $($r.topics_created) | Superseded (downvoted): $($r.superseded)" -ForegroundColor Gray
        if ($r.inserted -gt 0) {
            Write-Host "   Run Export-MemoryContext to surface them in the bridge." -ForegroundColor DarkGray
        }
    }
    return $r
}

function Sync-Memento {
    <#
    .SYNOPSIS   Bidirectional sync between LTM (memory.db) and Memento (memento.db).
    .EXAMPLE    Sync-Memento
    .EXAMPLE    Sync-Memento -Direction "memento_to_ltm"
    #>
    param(
        [ValidateSet('both','memento_to_ltm','ltm_to_memento')]
        [string]$Direction = "both"
    )
    $driver = Join-Path $PSScriptRoot "ltm_memento_bridge.py"
    if (-not (Test-Path $driver)) {
        throw "ltm_memento_bridge.py not found at $driver"
    }
    $payload = @{ direction = $Direction } | ConvertTo-Json -Compress
    $result = $payload | python $driver
    if ($LASTEXITCODE -ne 0) {
        throw "ltm_memento_bridge.py failed: $result"
    }
    $parsed = $result | ConvertFrom-Json
    Write-Host "🔄 Memory bridge sync complete ($Direction)"
    if ($parsed.memento_to_ltm) {
        Write-Host "   Memento → LTM: $($parsed.memento_to_ltm.facts_imported) facts, $($parsed.memento_to_ltm.patterns_imported) patterns"
    }
    if ($parsed.ltm_to_memento) {
        Write-Host "   LTM → Memento: $($parsed.ltm_to_memento.entities_exported) entities, $($parsed.ltm_to_memento.facts_exported) facts"
    }
    $parsed
}

function Invoke-MemoryMaintenance {
    <#
    .SYNOPSIS   Retention and compaction — keeps the store fast as topics accumulate.
    .DESCRIPTION
                Archives topics idle beyond -StaleTopicDays (protecting any that
                still have fresh facts), prunes superseded/disproven/resolved facts
                colder than -PruneDays, removes byte-identical duplicates, then
                checkpoints the WAL, VACUUMs and re-ANALYZEs.

                DRY RUN BY DEFAULT — pass -Apply to actually write.
    .EXAMPLE    Invoke-MemoryMaintenance                 # report only
    .EXAMPLE    Invoke-MemoryMaintenance -Apply
    #>
    param(
        [switch]$Apply,
        [int]$StaleTopicDays = 180,
        [int]$PruneDays      = 90
    )
    $driver = Join-Path $PSScriptRoot "ltm_maintain.py"
    if (-not (Test-Path $driver)) { throw "ltm_maintain.py not found at $driver" }

    $payload = @{ apply=[bool]$Apply; stale_topic_days=$StaleTopicDays; prune_days=$PruneDays } |
               ConvertTo-Json -Compress
    $out = $payload | python $driver
    if ($LASTEXITCODE -ne 0) { throw "ltm_maintain.py failed: $out" }
    $r = $out | ConvertFrom-Json
    if ($r.error) { throw "maintenance failed: $($r.error)" }

    $mode = if ($r.dry_run) { "DRY RUN (use -Apply to execute)" } else { "APPLIED" }
    Write-Host "🧹 Memory maintenance — $mode"
    Write-Host "   Topics to archive : $($r.archive_stale_topics.count) (idle > $($r.archive_stale_topics.threshold_days)d)"
    Write-Host "   Facts to prune    : $($r.prune_superseded.count) (retired > $($r.prune_superseded.threshold_days)d)"
    Write-Host "   Duplicates        : $($r.dedupe_facts.count)"
    if ($r.compact.applied) {
        Write-Host "   Space reclaimed   : $($r.compact.reclaimed_mb) MB"
    }
    Write-Host "   Active facts      : $($r.before.facts_active) → $($r.after.facts_active)"
    $r
}

function Backup-Memory {
    <#
    .SYNOPSIS   Take a verified, consistent snapshot of memory.db.
    .DESCRIPTION
                Uses SQLite's online backup API, not a file copy — memory.db runs
                in WAL mode, so a plain copy can capture a torn database whose
                committed transactions still live in the -wal sidecar. Each
                snapshot is integrity-checked immediately; a backup that fails
                verification is discarded rather than silently kept.
    .EXAMPLE    Backup-Memory
    .EXAMPLE    Backup-Memory -Keep 30 -MirrorDir "$env:OneDrive\ltm-backups"
    #>
    param(
        [int]$Keep = 14,
        [string]$BackupDir = "",
        [string]$MirrorDir = ""
    )
    $p = @{ op="backup"; keep=$Keep }
    if ($BackupDir) { $p.backup_dir = $BackupDir }
    if ($MirrorDir) { $p.mirror_dir = $MirrorDir }
    $r = Invoke-LtmBackup $p
    if ($r.ok) {
        Write-Host "💾 Backup verified → $($r.path)"
        Write-Host "   Facts: $($r.counts.facts) | Topics: $($r.counts.topics) | Entities: $($r.counts.entities)"
        if ($r.rotated.removed.Count) { Write-Host "   Rotated out: $($r.rotated.removed.Count) old snapshot(s)" }
        if ($r.mirror) { Write-Host "   Mirrored → $($r.mirror.path)" }
    } else {
        Write-Warning "Backup failed: $($r.error)"
    }
    $r
}

function Get-MemoryBackupStatus {
    <#
    .SYNOPSIS   Business-continuity posture: HEALTHY / DEGRADED / AT RISK.
    .EXAMPLE    Get-MemoryBackupStatus
    #>
    param([string]$BackupDir = "")
    $p = @{ op="status" }
    if ($BackupDir) { $p.backup_dir = $BackupDir }
    $r = Invoke-LtmBackup $p
    $icon = switch ($r.posture) { "HEALTHY" {"✅"} "DEGRADED" {"⚠️"} default {"🚨"} }
    Write-Host "$icon Continuity: $($r.posture) — $($r.detail)"
    if ($r.rpo_hours -ne $null) { Write-Host "   RPO: $($r.rpo_hours)h | Backups retained: $($r.backup_count)" }
    $r
}

function Restore-Memory {
    <#
    .SYNOPSIS   Restore memory.db from a verified backup.
    .DESCRIPTION
                Defaults to the newest snapshot. Refuses to restore a backup that
                fails verification, and saves the current database to a
                .pre-restore-<stamp> copy before overwriting. Requires -Confirm.
    .EXAMPLE    Restore-Memory                     # shows what WOULD happen
    .EXAMPLE    Restore-Memory -Confirm
    #>
    param(
        [string]$BackupPath = "",
        [string]$BackupDir  = "",
        [switch]$Confirm
    )
    $p = @{ op="restore"; confirm=[bool]$Confirm }
    if ($BackupPath) { $p.backup = $BackupPath }
    if ($BackupDir)  { $p.backup_dir = $BackupDir }
    $r = Invoke-LtmBackup $p
    if ($r.requires_confirm) {
        Write-Host "🔍 Would restore: $($r.would_restore)"
        Write-Host "   Into         : $($r.into)"
        Write-Host "   Contains     : $($r.verify.counts.facts) facts, $($r.verify.counts.topics) topics"
        Write-Host "   Re-run with -Confirm to proceed."
    } elseif ($r.ok) {
        Write-Host "♻️  Restored from $($r.restored_from)"
        Write-Host "   Facts: $($r.verify.counts.facts) | Previous DB saved to $($r.pre_restore_copy)"
    } else {
        Write-Warning "Restore failed: $($r.error)"
    }
    $r
}

function Invoke-LtmBackup {
    param([Parameter(Mandatory)][hashtable]$Payload)
    $driver = Join-Path $PSScriptRoot "ltm_backup.py"
    if (-not (Test-Path $driver)) { throw "ltm_backup.py not found at $driver" }
    $out = ($Payload | ConvertTo-Json -Compress) | python $driver
    if ($LASTEXITCODE -ne 0) { throw "ltm_backup.py failed: $out" }
    $out | ConvertFrom-Json
}

function Export-MemoryVersion {
    <#
    .SYNOPSIS   Write a datetime-versioned, self-describing snapshot of memory to a folder.
    .DESCRIPTION
                Produces a directory named <Destination>\<yyyyMMdd-HHmmss> containing:
                  memory-<stamp>.db          verified binary snapshot (online backup API)
                  memory-<stamp>.json        portable human-readable export
                  memory-context-<stamp>.md  the rendered bridge at this point in time
                  MANIFEST.md                counts, integrity result, restore instructions

                Unlike Backup-Memory (rotating operational backups), this is an
                archival, self-contained version you can keep, diff or hand off.
    .EXAMPLE    Export-MemoryVersion -Destination C:\workspace\agency\memory
    #>
    param(
        [Parameter(Mandatory)][string]$Destination,
        [string]$Label = ""
    )

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $dir   = Join-Path $Destination $stamp
    New-Item -ItemType Directory -Path $dir -Force | Out-Null

    # 1. Verified binary snapshot (WAL-safe online backup, not a file copy).
    $bk = Invoke-LtmBackup @{ op="backup"; backup_dir=$dir; keep=999 }
    if (-not $bk.ok) { throw "snapshot failed: $($bk.error)" }
    $dbPath = Join-Path $dir "memory-$stamp.db"
    if ($bk.path -ne $dbPath) { Move-Item $bk.path $dbPath -Force }

    # Fold any WAL content back into the file and drop the sidecars, so the
    # archived .db is genuinely self-contained rather than a 3-file set.
    $consolidate = @"
import sqlite3, os
p = r'$dbPath'
c = sqlite3.connect(p, isolation_level=None)
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.execute('PRAGMA journal_mode=DELETE')
c.close()
for side in (p + '-wal', p + '-shm'):
    if os.path.exists(side):
        os.remove(side)
"@
    $consolidate | python -
    if ($LASTEXITCODE -ne 0) { throw "failed to consolidate WAL into snapshot" }

    # 2. Portable JSON exports: full (restorable) + flat (for other harnesses).
    $jsonPath = Join-Path $dir "memory-$stamp.json"
    $exporter = Join-Path $PSScriptRoot "memory_export.py"
    python $exporter --output $jsonPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "memory_export.py failed" }

    $portablePath = Join-Path $dir "memory-$stamp.portable.json"
    python $exporter --portable --output $portablePath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "memory_export.py --portable failed" }

    # 3. Rendered bridge as it stands right now.
    $ctxPath = Join-Path $dir "memory-context-$stamp.md"
    $null = Export-MemoryContext
    Copy-Item $script:MemoryContextFile $ctxPath -Force

    # 4. Manifest.
    $stats  = Invoke-Memory @{ op="get_stats" }
    $verify = Invoke-LtmBackup @{ op="verify"; path=$dbPath }
    $sizeDb   = [math]::Round((Get-Item $dbPath).Length / 1KB, 1)
    $sizeJson = [math]::Round((Get-Item $jsonPath).Length / 1KB, 1)
    $sizePort = [math]::Round((Get-Item $portablePath).Length / 1KB, 1)

    $manifest = @"
# Memory Version — $stamp

$(if ($Label) { "**Label:** $Label`n" })**Exported:** $(Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz")
**Source:** $($script:MemoryDb)
**Schema version:** $($stats.schema_version)

## Contents

| File | Size | Purpose |
|------|------|---------|
| ``memory-$stamp.db`` | $sizeDb KB | Verified binary snapshot — **restore this** |
| ``memory-$stamp.json`` | $sizeJson KB | Full table-shaped export — restorable via ``memory_restore.py`` |
| ``memory-$stamp.portable.json`` | $sizePort KB | **Flat, join-free — use this in another agent harness** |
| ``memory-context-$stamp.md`` | — | The bridge agents read, as rendered at export time |

## Row counts

| Table | Rows |
|-------|------|
| topics | $($stats.topics) |
| facts | $($stats.facts) |
| entities | $($stats.entities) |
| snapshots | $($stats.snapshots) |
| refs | $($stats.refs) |

## Integrity

- ``PRAGMA integrity_check``: **$($verify.integrity)**
- Foreign-key violations: **$($verify.foreign_key_violations)**
- Verified: **$($verify.ok)**

## Restore

``````powershell
. C:\workspace\agency\long-term-memory\memory.ps1
Restore-Memory -BackupPath "$dbPath"            # dry run
Restore-Memory -BackupPath "$dbPath" -Confirm   # execute
``````

The full JSON is restorable independently via ``memory_restore.py`` if the
binary snapshot is ever unusable — two independent recovery paths.

## Using this in another agent harness

Read ``memory-$stamp.portable.json``. Top-level keys: ``identity``, ``topics``,
``facts``, ``entities``, ``snapshots``, ``patterns``, ``sessions``, ``counts``.
Facts already carry a resolved ``topic`` slug, so no joins are needed:

``````json
{ "content": "...", "type": "finding", "importance": 5,
  "confidence": 5, "status": "active", "source": "...",
  "topic": "fy27-comp-plan", "last_accessed_at": "..." }
``````

Filter on ``status == "active"`` for current knowledge, and sort by
``importance`` (5 = load always) to build that harness's own context window.
"@
    $manifest | Out-File (Join-Path $dir "MANIFEST.md") -Encoding utf8

    Write-Host "📦 Memory version exported → $dir"
    Write-Host "   Facts: $($stats.facts) | Topics: $($stats.topics) | Entities: $($stats.entities) | Snapshots: $($stats.snapshots)"
    Write-Host "   Integrity: $($verify.integrity) | DB $sizeDb KB | JSON $sizeJson KB"

    [pscustomobject]@{
        path = $dir; stamp = $stamp; db = $dbPath; json = $jsonPath
        portable = $portablePath; context = $ctxPath; stats = $stats; verify = $verify
    }
}

Write-Host "🧠 Memory module loaded | DB: $script:MemoryDb"
