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

    $fact = Add-Fact -Content $Content -TopicSlug $TopicSlug -FactType $FactType `
                     -Confidence $Confidence -Importance $Importance -Source $Source
    $ctx = Export-MemoryContext

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
        Write-Host "🔁 FTS reindexed → $($r.reindexed.fts_rows) rows | schema v$($r.schema_version)"
    } else {
        Write-Host "   FTS already at schema v$($r.schema_version) — no reindex needed"
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

Write-Host "🧠 Memory module loaded | DB: $script:MemoryDb"
