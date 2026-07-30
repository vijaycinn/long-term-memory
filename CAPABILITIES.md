# CAPABILITIES — current state and extension guide

**Audience: agents.** Read this before changing anything in this repository. It
describes what the long-term memory store does today, the defects that have
already been fixed and must not be reintroduced, the invariants that hold the
design together, and how to extend it safely.

| | |
|---|---|
| **Schema version** | **3** |
| Driver ops | 15 |
| PowerShell cmdlets | 24 |
| Tests | **130** across 6 files |
| Session start @ 100k facts | **161 ms** |
| Continuity | RPO ≤ 12 h unattended, RTO seconds |

---

## 1. Architecture in one pass

```
  Agent / user
       │
       ▼
  memory.ps1 ............ PowerShell surface (24 cmdlets)
       │  JSON over stdin/stdout, one python process per call
       ▼
  memory_driver.py ...... op dispatch (15 ops)
       │
       ▼
  ~/.copilot/memory.db .. SQLite + FTS5
       │
       ▼
  ~/.copilot/memory-context.md ....... the "bridge"
  ~/.copilot/copilot-instructions.md . LTM block agents actually load
```

**The bridge is the product.** Everything else exists so that
`memory-context.md` contains the right ~40 facts at session start. Storage is
cheap; *selection* is the hard part.

### Data model

| Table | Role | Notes |
|-------|------|-------|
| `topics` | Named subjects of ongoing interest | `slug` is the human handle used everywhere |
| `facts` | Atomic knowledge units — the core cell | `topic_id` NULL = global fact |
| `entities` | People, accounts, products, tools | `is_self = 1` row **is** the identity block |
| `entity_mentions` | Polymorphic links entity → fact/topic/snapshot | |
| `snapshots` | Immutable point-in-time topic summaries | never UPDATE; INSERT a new `seq_number` |
| `refs` | External references | |
| `memory_fts` | FTS5 index over facts/entities/topics/snapshots | kept in sync by triggers |

**Fact lifecycle:** `active` → `superseded` / `disproven` / `resolved` → pruned
by maintenance once cold. **Retire, don't delete** — `export_context` only reads
`active`, so superseding is instant and reversible.

---

## 2. Op reference (`memory_driver.py`)

| Op | Purpose |
|----|---------|
| `add_topic` | Upsert a topic. Sets `last_accessed_at`. |
| `get_topic` | Fetch topic metadata by slug. |
| `add_fact` | Insert a fact, deduped. Accepts `topic_id` **or** `topic_slug`. Sets `last_accessed_at`. |
| `add_entity` | Upsert an entity. |
| `add_snapshot` | Append an immutable snapshot (per-topic `seq_number`). |
| `set_identity` | Set the `is_self` entity; guarantees exactly one. |
| `touch_fact` / `touch_topic` | Refresh recency to resurface something older. |
| `search_memory` | FTS search. Auto-recovers from FTS5 operator syntax. |
| `export_context` | Render the bridge. Per-topic quota; all limits tunable. |
| `batch` | Run several ops in **one** interpreter start. |
| `migrate` | Idempotent upgrade + repair to the current schema. |
| `reindex_fts` | Rebuild the FTS index (v2 topic-slug indexing). |
| `optimize_db` | Apply v3 guarded triggers + covering indexes. |
| `get_stats` | Row counts and schema version. |

### PowerShell surface

Capture: `Add-Fact`, `Remember-This`, `Add-Topic`, `Add-Entity`, `Add-Snapshot`, `Set-Identity`
Retrieve: `Search-Memory`, `Get-Topic`, `Get-MemoryStats`, `Matrix-Load`, `Export-MemoryContext`
Maintain: `Invoke-MemoryMigration`, `Invoke-MemoryMaintenance`, `Lint-Memory`, `Export-Wiki`
Continuity: `Backup-Memory`, `Get-MemoryBackupStatus`, `Restore-Memory`, `Export-MemoryVersion`

---

## 3. Fixes already made — do not reintroduce these

Every entry below was a real defect observed in a live session, and every one is
now covered by a test. **If you change the relevant code, run the tests.**

### Schema v2 — relevance

| # | Defect | Root cause | Fix | Guarded by |
|---|--------|-----------|-----|-----------|
| 1 | `Search-Memory -Query 'remember-this'` threw `no such column: this` | `-` and `:` are FTS5 operators; the query was passed through verbatim | Run verbatim first (preserves `OR` / `NOT` / `"phrase"` / `prefix*`), auto-quote bare tokens and retry only on rejection | `TestFtsOperatorTerms` |
| 2 | New facts never appeared in the bridge | `add_fact` left `last_accessed_at` NULL, and **NULLs sort last** under `ORDER BY ... DESC` | Set on insert; re-asserting refreshes it. Backfill repaired **274 of 298** live facts | `TestFactRecency` |
| 3 | Importance-4 facts written but unreachable | One global `importance DESC LIMIT 20` — 20 importance-5 facts starved everything else | Per-topic quota + reserved importance-5 pool; limits tunable | `TestExportFairness` |
| 4 | Entities / snapshots / todos stored but invisible | Exporter rendered only identity, topics, facts | Added Known Entities, Where I Left Off, Pending Work | `TestExportSections` |
| 5 | Identity went stale with no obvious fix | There is **no `identity` table** — it is `entities.is_self` | `set_identity`, upserts in place, one self row | `TestSetIdentity` |
| 6 | `Search-Memory 'fy27-comp-plan'` found nothing | Topics indexed as `title + description`; the slug was excluded | Slug added to FTS + `fts_resolved` | `TestSlugSearchable` |
| 7 | A fact containing `## Heading` corrupted the bridge | Bodies rendered verbatim became real document headings | All rendered values flattened to one line | `TestMarkdownSafety` |
| 8 | `unittest discover` failed with `I/O operation on closed file` | `sys.stdin` rewrapped at **import** time in 3 modules | Moved under `__main__`. Had been masking the entire wiki-export suite | full suite |

### Schema v3 — performance

| # | Defect | Root cause | Fix | Guarded by |
|---|--------|-----------|-----|-----------|
| 9 | **`export_context` took 4.3 s at 50k facts** — at every session start | Recency bumps fired `fts_facts_update`, whose `DELETE ... WHERE row_id = ?` is a **full scan** (`memory_fts.row_id` is `UNINDEXED`). ~2M scanned rows per session start | `WHEN OLD.content IS NOT NEW.content` guard | benchmark + suite |
| 10 | A second write per recency touch | `facts_updated` bumped `updated_at` on every update | Guarded to content-bearing columns | — |
| 11 | Search grew linearly | Joined `memory_fts` against `fts_resolved`, a 4-way `UNION ALL` over every row, to return 10 | Fetch hit ids first, resolve with bounded `IN (...)` | benchmark |
| 12 | Filesorts on export queries | No covering indexes | `idx_facts_topic_rank`, `idx_facts_status_rank`, `idx_facts_pending`, `idx_entities_recent` | — |
| 13 | N individual recency `UPDATE`s | Per-row loop | One batched statement per table | — |
| 14 | **~93 % of wall time was Python process spawn** | Every call starts a new interpreter (~519 ms) while DB work is ~39 ms | `batch` op; `Remember-This` 1543 ms → 676 ms | `TestBatch` |
| 15 | Two backups in the same second **silently overwrote** each other | Second-granular filename collision — a lost recovery point | Collision-proof paths | `TestBackup` |
| 16 | `topic_slug` in a batch silently created a **global** fact | `add_fact` only accepted `topic_id` | `add_fact` resolves `topic_slug` | `TestBatch` |
| 17 | `memory_export.py` crashed on Windows *after* writing the file | Emoji printed to a cp1252 console → `UnicodeEncodeError`, so success looked like failure | Force UTF-8 stdout | — |

---

## 4. Invariants — breaking these breaks the system

1. **Never let `last_accessed_at` be NULL on a live row.** NULLs sort last; the
   row becomes invisible to `export_context`. `migrate` backfills.
2. **Never remove the `WHEN` guards on the update triggers.** Without them every
   recency bump re-indexes FTS and session start returns to O(database).
3. **`memory_fts.row_id` is `UNINDEXED`** — any `WHERE row_id = ?` against it is
   a full scan. Only acceptable on genuine content change.
4. **Exactly one `entities.is_self = 1` row.** Use `set_identity`.
5. **Snapshots are immutable.** Insert a new `seq_number`; never UPDATE.
6. **Flatten anything rendered into the bridge** (`_md_inline`). Stored text is
   free-form and will contain markdown.
7. **`UNIQUE(topic_id, content)` does not catch global facts** — SQLite treats
   `NULL != NULL`. Pre-check with `IS`, and let maintenance dedupe.
8. **Retire, don't delete.** Set `status='superseded'`.
9. **Minimise call count, not query size.** Process spawn dominates. Use `batch`.
10. **Hooks must never raise.** `ltm_session_end` swallows everything.

---

## 5. Performance — and how to re-measure

```powershell
python ltm_bench.py --sizes 1000,10000,50000,100000
```

| facts | topics | session start | search (selective) | `add_fact` |
|------:|-------:|--------------:|-------------------:|-----------:|
| 1 000 | 25 | 43 ms | 5 ms | 33 ms |
| 10 000 | 100 | 48 ms | 5 ms | 37 ms |
| 50 000 | 500 | 103 ms | 7 ms | 39 ms |
| 100 000 | 1 000 | **161 ms** | **7 ms** | 33 ms |

The harness prints a `linear` / `sub-linear` / `SUPER-LINEAR` verdict.
**If it ever prints `SUPER-LINEAR`, you have regressed a hot path.**

---

## 6. Operations

```powershell
Invoke-MemoryMaintenance          # dry run — always look first
Invoke-MemoryMaintenance -Apply   # archive idle topics, prune cold retired facts, dedupe, VACUUM
Backup-Memory                     # verified snapshot (online backup API, WAL-safe)
Get-MemoryBackupStatus            # HEALTHY / DEGRADED / AT RISK
Restore-Memory -Confirm           # restore newest verified snapshot
Export-MemoryVersion -Destination <dir>   # archival, self-contained versioned export
```

`Export-MemoryVersion` writes `<dir>\<yyyyMMdd-HHmmss>\` containing a verified
`.db`, a portable `.json`, the rendered bridge, and a `MANIFEST.md` — **two
independent recovery paths** from one export.

---

## 7. Extending safely

### Add a new op
1. Write the function in `memory_driver.py` taking a single payload dict; use
   `_db_path(p)` so tests can point at a temp database.
2. Register it in `DISPATCH` — it then works inside `batch` for free.
3. Add a `Invoke-Memory @{ op="..." }` wrapper in `memory.ps1` with comment help.
4. Add a test class in `test_memory_driver.py`.

### Add a bridge section
Extend `export_context`: query it, add a `pending_limit`-style tunable, render
through `_md_inline`, and return a `*_loaded` count. Assert the section in
`TestExportSections`.

### Change the schema
1. Edit `init-memory.sql` **and** bump `SCHEMA_VERSION`.
2. Add an in-place upgrade to `migrate` — existing databases keep old
   `CREATE ... IF NOT EXISTS` objects, so triggers/views must be **dropped and
   recreated** explicitly.
3. Test the upgrade from the *old* shape, not just the new one — see
   `test_migrate_upgrades_legacy_index`.

### Ideas worth adding
- Semantic/embedding search alongside FTS for concept recall
- Fact confidence decay over time
- Automatic contradiction resolution (`ltm_lint` detects; nothing resolves)
- Cross-machine sync beyond `-MirrorDir`
- Per-topic importance calibration so hot topics don't inflate

---

## 8. Testing

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -W ignore::ResourceWarning -m unittest discover -p "test_*.py"
```

| File | Tests | Covers |
|------|------:|--------|
| `test_memory_driver.py` | 44 | FTS sanitizing, recency, export fairness, identity, migration, batch, markdown safety |
| `test_ltm_ops.py` | 35 | Retention, compaction, backup, **destroy-and-restore drill**, auto-backup |
| `test_wiki_export.py` | 20 | Wiki export |
| `test_lint.py` | 14 | Health checks |
| `test_session_start.py` | 9 | sessionStart hook |
| `test_session_end.py` | 8 | sessionEnd hook |
| **Total** | **130** | |

**A change to trigger definitions, `export_context`, or `search_memory` must be
accompanied by a benchmark run.** The tests prove correctness; only the
benchmark proves you did not reintroduce defect #9.
