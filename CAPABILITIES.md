# CAPABILITIES — current state and extension guide

**Audience: agents.** Read this before changing anything in this repository. It
describes what the long-term memory store does today, the defects that have
already been fixed and must not be reintroduced, the invariants that hold the
design together, and how to extend it safely.

| | |
|---|---|
| **Schema version** | **3** |
| Driver ops | 15 |
| PowerShell cmdlets | 26 |
| Tests | **296** unittest across 13 files, plus 2 standalone harnesses |
| Session start @ 100k facts | **161 ms** |
| Continuity | RPO ≤ 12 h unattended, RTO seconds |

---

> **Two traps for the next agent.** Tool output redacts credential-adjacent
> lines: `view`/`grep` can make `wiki/vault_embed.py`'s `Authorization` header
> look like a literal placeholder even when the file on disk is correct. Do not
> "fix" it from rendered output; inspect properties in Python (length, variable
> names, or a hash) without printing the credential. Also, `_render_ranked`
> right-aligns its `{marker:>10}` field, so result lines begin with spaces.
> Parsers must allow leading whitespace; `line.startswith("[")` returns zero
> results and produces a false benchmark.

---

## 1. Architecture in one pass

```
  Agent / user
       │
       ▼
  memory.ps1 ............ PowerShell surface (26 cmdlets)
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

The base repo is standard-library Python. `pyyaml` is the only repo-level
third-party import and is guarded by `try/except` in
`wiki/check_frontmatter.py`; `sqlite-vec` is an optional extension used only by
the vector sidecar. Do not add a required dependency to the retrieval path.

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
| `sync_agent_memory` | Replay harness `store_memory` calls from the session event log into this store. Honours later `vote_memory` downvotes. |

### PowerShell surface

Capture: `Add-Fact`, `Remember-This`, `Add-Topic`, `Add-Entity`, `Add-Snapshot`, `Set-Identity`
Retrieve: `Search-Memory`, `Get-Topic`, `Get-MemoryStats`, `Matrix-Load`, `Export-MemoryContext`
Maintain: `Invoke-MemoryMigration`, `Invoke-MemoryMaintenance`, `Lint-Memory`, `Export-Wiki`
Continuity: `Backup-Memory`, `Get-MemoryBackupStatus`, `Restore-Memory`, `Export-MemoryVersion`
Sync: `Sync-AgentMemory` (`-WhatIf` to preview, `-SinceHours 0` for a full backfill)
Wiki: `Sync-Vault` (`-Full` to rescan the workspace, `-Force`, `-Status`), `Request-VaultSync`

### The knowledge wiki (`wiki/`)

Memory answers one fact at a time; the wiki compiles memory + the session store
+ an optional source workspace into a cross-referenced Obsidian vault, following
Karpathy's LLM-wiki pattern. See the README section *The knowledge wiki* and
`wiki/templates/AGENTS.md` (the operating contract seeded into every vault).

Most memory writes call `Request-VaultSync`, which drops a marker; the rebuild
runs on `Export-MemoryContext`, `Matrix-Load`, `Sync-Vault`, or the sessionEnd
hook. `Remember-This` is the deliberate synchronous exception: it batches and
validates the memory results, marks the vault dirty, runs an incremental
`Sync-Vault`, and waits for the configured vector step before returning.

The percolation order is: new fact in `memory.db` FTS5 immediately, generated
Obsidian pages next, then sidecar BM25 and sqlite-vec when the semantic layer is
configured. Its result exposes `page_state`, `lexical_current`, `index_state`,
`index_configured`, `semantic_current`, and `index_reason`. `-NoIndex` (or
`LTM_VAULT_AUTOINDEX=0`) returns a current lexical page with
`index_state = disabled`; unavailable vectors return `index_state = unavailable`
and the write still succeeds. A configured failure or partial index returns `failed`/`partial`; the fact stays
committed, but `Remember-This` throws and retains the dirty marker for retry.

**Query surface.** `wiki/vault_mcp.py` is an MCP server exposing `memory_search`
(FTS5 straight against `memory.db` — works with no vault and no Obsidian) plus
`vault_search`, `vault_read`, `vault_index`, `vault_recent` and
`vault_file_answer`. MCP tools sit in the agent's tool list on every turn, which
is why this is more reliable than an instruction or a skill.

**Three modes**, decided purely by what exists on disk: `empty` (no database —
tools report and exit cleanly), `memory-only` (a full wiki from memory alone),
`full` (plus workspace documents). Paths resolve env var →
`~/.copilot/wiki-config.json` → default, so nothing is machine-specific.
Proven by `wiki/test_bootstrap.py`, which exercises all three in a temp
directory.

### Retrieval v4 surface

`vault_read` accepts `hops=1`. It follows outbound `[[wikilink]]` edges and
appends a `## Linked pages` block containing neighbour titles and one-line
summaries, with inline body links before auto-generated `## Related` links.
Neighbour bodies are not inlined. On the golden set, `hops=1` did not improve
recall: direct search and `vault_current_hops1` both hit **18/27 (66.7%)** at
recall@5. Treat it as an exploration aid, not a hit-rate improvement.

`vault_search` accepts `mode` with these values:

| Mode | Behaviour |
|---|---|
| `hybrid` | Default: BM25 plus vector results fused by RRF, then page-level reranked |
| `hybrid-rrf` | Raw RRF order; bypasses the reranker |
| `vector` | Vector-only page search |
| `bm25` | Sidecar FTS5 BM25 page search |
| `lexical` | Frozen `str.count()` substring scorer; guaranteed offline fallback |

Semantic modes are optional. Missing `sqlite-vec`, an empty index, an unset
endpoint, a lapsed `az login`, or another semantic-path failure degrades to
`lexical`. The diagnostic is:

```powershell
python wiki\vault_vectors.py status
az login
```

`wiki/vault_vectors.py` stores the sidecar at the path resolved by
`config.paths()`, chunks cleaned pages at about **1,200 characters with 200
characters of overlap**, invalidates by content hash, and caches query
embeddings in `query_cache`. The remote embedding round trip measured about
**650 ms of a ~667 ms hybrid p50**. The recorded scale is **7,940 chunks over
619 pages**, **512 dimensions**, model `text-embedding-3-small`. Its CLI is:

```powershell
python wiki\vault_vectors.py status
python wiki\vault_vectors.py search "content understanding cost" --limit 5
```

`Sync-Vault` runs `index` automatically after a successful page rebuild.
`python wiki\vault_vectors.py index` remains available for maintenance or
recovery; `--quiet` suppresses progress. `--limit-pages N` is a scoped update
for smoke testing: it preserves chunks and BM25 rows outside the selected page
set, guarded by `test_limited_index_preserves_out_of_scope_chunks`.

`wiki/vault_rerank.py` is stdlib-only and operates at page level over the
fused candidate pool. It uses normalised RRF **0.60**, title coverage **0.20**,
page semantic similarity from a precomputed chunk-vector centroid **0.08**,
path-class prior **0.07**, dual-retriever corroboration **0.03**, and length
normalisation **0.02**. `MIN_TITLE_COVERAGE = 0.50` returns raw RRF when no
candidate has strong title evidence. The oracle artifact records **21/27
(77.8%) Recall@20** for the fused candidate pool; six targets are outside that
pool, so no reranker can retrieve them. Paraphrase recall@5 remains **3/11
(27.3%)**.

`Search-Memory` and MCP `memory_search` both use
`core/memory_driver.py::score_fts_hit` and `blend_fts_hits`, so their result
ordering is identical. This resolves the former driver-versus-MCP divergence
where the driver was pure BM25 and MCP blended importance.

`ops/backfill_entity_mentions.py` has `--dry-run` and `--apply`. It backfills
fact-to-entity edges without changing facts; `--apply` backs up before
inserting. Run it because `memory_search` returns no `related_facts` on a
database that has never had the backfill (or session-end edge capture) run.

`ops/retrieval_bench.py` supports `--baseline OUT.json`, `--compare A.json
B.json`, `--oracle`, `--paths` and `--json`. Strategies are `lexical_v0`,
`memory_v0`, `vault_current`, `memory_current` and `vault_current_hops1`.
`--oracle` measures the fused candidate-pool ceiling and writes
`docs\bench\oracle-ceiling-2026-09-02.json`.

Entirely optional. When the wiki layer is absent every entry point is a no-op,
and the sessionEnd hook is best-effort — a wiki failure can never break the hook
or lose memory data.

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
| 18 | JSON export silently dropped **194 rows** of behavioural memory | The table list omitted `ltm_patterns` (138) and `ltm_session_log` (56); only `memory_fts*` should be excluded, since that alone is derived | Both added; portable export exposes them as `patterns` / `sessions` | — |
| 19 | 64 valid embedding inputs returned HTTP 400 | Azure applies an aggregate token budget to the request; 64 ~1,200-character chunks exceeded it | Recursively bisect HTTP-400 batches while preserving result order | `EmbedFallbackTests` |
| 20 | Switching Azure subscriptions left a valid cached token for the wrong tenant | Token cache lifetime exceeded the active-subscription context | On the specific tenant-mismatch 400, clear and refresh the token once | `EmbedFallbackTests` |
| 19 | `add_snapshot` with `topic_slug` raised `KeyError('topic_id')` and the snapshot was **silently lost** | Same gap as #16 in a different op — only `topic_id` was accepted | Resolves `topic_slug`; returns a clear error for an unknown slug | `TestBatch` |
| 20 | `touch_topic` took `slug` while `add_fact` / `add_snapshot` took `topic_slug` | Inconsistent key naming across topic-referencing ops — same trap, but at least it failed loudly | `topic_slug` accepted as an alias; unknown slug now errors instead of silently updating nothing | `TestTopicReferenceContract` |
| 21 | **`vault_mcp` `memory_search` returned the wrong facts** — every hit resolved to a different record than the one that matched (measured 5/5 wrong). Worse than empty results, since it reads as a confident answer | `_fts_query` joined `facts.id = memory_fts.rowid`. `memory_fts` is a **combined** index over facts/entities/topics/snapshots, so its implicit rowid is its own sequence (924 rows, max 1127) and is unrelated to `facts.id` (669 rows, max 683). This violates the rule already stated in `init-memory.sql`: *row_id is only unique within a table_name bucket — never join on row_id alone*. It shipped because `wiki/test_bootstrap.py` declared a simplified `fts5(content)` keyed by rowid, so the wrong join passed in tests; and `test_vault_mcp.py` never exercised `memory_search` at all | Join on `m.row_id AND m.table_name = 'facts'`. Fixture now mirrors the production 3-column schema and offsets the FTS rowid sequence so a rowid join cannot line up by coincidence | `test_vault_mcp.py` id=9 — asserts every returned fact **contains the search term**, not merely that a response arrived (mutation-tested: reintroducing the join fails 4/5) |

### Store divergence

| # | Defect | Root cause | Fix | Guarded by |
|---|--------|-----------|-----|-----------|
| 21 | **112 facts saved by the agent were missing from the store.** Anything captured with the harness `store_memory` tool never reached the bridge, so it was invisible at session start — while `Add-Fact` writes were invisible to the harness. Two stores diverging in silence, and *both* looked healthy in isolation | `store_memory` writes to the harness's own memory service. `export_context` reads SQLite. Nothing connected them | `sync_agent_memory` replays `store_memory` calls out of `session-state/<id>/events.jsonl`, the one place they are recorded locally. Wired into `ltm_session_end` (48 h window) so it stays in sync unattended | `TestSyncAgentMemory` |
| 22 | The first cut of #21 **evicted every genuinely active topic from the bridge** | Imported rows were stamped `now()`, so all 68 imported topics looked freshly used and won the `ORDER BY last_accessed_at` | Use the event's own timestamp; a re-sync corrects recency *backwards* but never bumps it forward, since replaying history is not a fresh relevance signal | `test_recency_uses_event_time_not_sync_time`, `test_resync_does_not_bump_recency_forward` |
| 23 | A re-sync would have **resurrected facts the agent had retracted** | `vote_memory` downvotes live only in the harness store; a naive replay of the original `store_memory` call reinstates the stale claim | Downvotes are read from the same event log and the matching fact is set `status='superseded'` (invariant 8: retire, don't delete) | `test_downvoted_fact_is_retired_not_resurrected` |
| 24 | **Auto-extracted facts were stored but never rendered.** Found while auditing #21 — same invisibility, different cause | Both `extract_facts` and `extract_pending_work` inserted without `last_accessed_at`; NULLs sort last so `export_context` never reached them. The row existed, so every count and integrity check looked healthy | Both inserts set `datetime('now')`; `Invoke-MemoryMigration` backfills existing rows | `test_every_extracted_fact_is_visible_to_the_bridge` — asserts across *all* rows, so a third insert path cannot reintroduce it |
| 25 | **Searching for a filename, path, email or percentage crashed** — e.g. `ltm.instructions.md` raised `fts5: syntax error near "."`. Only `-` and `:` had ever worked, and the stored memory is full of paths and filenames | `_fts_sanitize` used a **denylist**, `set('-:^*()"')`. A denylist must enumerate every character FTS5 dislikes and fails on the first one it missed; the sanitized query came back unchanged, so the driver re-raised instead of retrying | Inverted to an **allowlist**: a bare token is left alone only if it is `^\w+\*?$`, otherwise it is quoted. Safe to be aggressive because sanitising only runs after the query has already been rejected. Tokenising now also keeps `"quoted phrases"` intact instead of splitting them on whitespace | `test_search_survives_every_punctuation_class` (20 classes), `test_dotted_term_finds_the_fact`, `test_sanitizer_leaves_a_valid_quoted_phrase_intact` |

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
11. **Imported memory carries the time it was stored, not the time it was
    imported.** Stamping `now()` on a replay makes stale topics look hot and
    pushes live work out of the bridge.
12. **Sanitise FTS queries with an allowlist, never a denylist.** A denylist has
    to enumerate every character FTS5 treats as syntax and fails on the first
    one it missed — `.` was missing for months, so searching for any filename
    crashed. Only a plain `^\w+\*?$` token is safe to leave bare.

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
`.db`, a full `.json`, a **flat `.portable.json`**, the rendered bridge, and a
`MANIFEST.md` — **two independent recovery paths** from one export.

### Consuming memory in a different agent harness

Use the `.portable.json` (`copilot-memory-portable-v1`), not the table-shaped
export. It resolves foreign keys and drops internal ids, so no joins are needed:

```json
{ "identity": { "name": "...", "attributes": "...", "notes": "..." },
  "facts": [ { "content": "...", "type": "finding", "importance": 5,
               "confidence": 5, "status": "active", "source": "...",
               "topic": "fy27-comp-plan", "last_accessed_at": "..." } ],
  "topics": [...], "entities": [...], "snapshots": [...],
  "patterns": [...], "sessions": [...], "counts": {...} }
```

Filter `status == "active"` for current knowledge; sort by `importance`
(5 = load always) to build that harness's own context window.

```powershell
python memory_export.py --portable --output harness-memory.json
```

> **The table-shaped export omits `memory_fts*` by design** — it is derived and
> rebuilt by `reindex_fts`. It does **not** omit `ltm_patterns` or
> `ltm_session_log`; those are behavioural memory and were previously being
> dropped silently (defect #18).

---

## 7. Extending safely

### Add a new op
1. Write the function in `memory_driver.py` taking a single payload dict; use
   `_db_path(p)` so tests can point at a temp database.
2. Register it in `DISPATCH` — it then works inside `batch` for free.
3. **If it references a topic, accept `topic_slug` and add it to
   `TestTopicReferenceContract.TOPIC_OPS`.** The slug is the handle used
   everywhere else; accepting only `topic_id` caused defects #16 and #19 and
   both failed *silently*.
4. Add a `Invoke-Memory @{ op="..." }` wrapper in `memory.ps1` with comment help.
5. Add a test class in `test_memory_driver.py`.

### Why per-op tests were not enough
Defects #16 and #19 were the same bug in two ops. Each op had a test, but every
test was written against that op's *current signature*, so it could never fail.
`TestTopicReferenceContract` tests the **shared contract** instead — it
enumerates topic-referencing ops, asserts each accepts a slug, asserts each
reports an unknown slug loudly, asserts the row actually lands on the right
topic, and includes a **tripwire** that fails if an op gains slug handling
without being registered. Verified by reintroducing defect #19: the contract
test goes from green to 1 failure + 3 errors.

**Lesson worth generalising: when you fix a defect, ask whether it is an
instance or a class.** If a second op could have the same bug, write a contract
test, not another per-op test.

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
- Fact confidence decay over time
- Automatic contradiction resolution (`ltm_lint` detects; nothing resolves)
- Cross-machine sync beyond `-MirrorDir`
- Per-topic importance calibration so hot topics don't inflate

---

## 8. Testing

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py" -t tests
python wiki\test_bootstrap.py
python wiki\test_vault_mcp.py
```

| File | Tests | Covers |
|------|------:|--------|
| `test_memory_driver.py` | 71 | FTS sanitizing, recency, export fairness, identity, migration, batch, markdown safety |
| `test_ltm_ops.py` | 35 | Retention, compaction, backup, **destroy-and-restore drill**, auto-backup |
| `test_wiki_export.py` | 20 | Wiki export |
| `test_vault_vectors.py` | 42 | Boilerplate stripping, chunking, content hashing, FTS sanitising, RRF fusion, degradation and safe limited indexing |
| `test_vault_embed.py` | 18 | Token expiry states, the `az login` nudge, HTTP-400 batch splitting, tenant-switch refresh, and fallback paths |
| `test_vault_mcp_rank.py` | 17 | **bm25 sign trap** (mutation-tested), rare-term ranking, candidate-pool direction, entity co-mention caps, `hops=0` byte-identity |
| `test_vault_rerank.py` | 4 | Page-level reranking signals, title guard, stable ordering and failure fallback |
| `test_backfill_entity_mentions.py` | 1 | Fact-level entity-edge backfill |
| `test_lint.py` | 14 | Health checks |
| `test_vault_sync.py` | 41 | Wiki bridge — sessionEnd hook, sync marker/lock/retry, concurrent-write preservation, lock cleanup, incremental scoping, auto-index and Remember-This percolation |
| `test_session_start.py` | 13 | sessionStart hook, incl. prompt-aware retrieval (empty/short/long/punctuation) |
| `test_session_end.py` | 10 | sessionEnd hook, incl. fact-level entity edges |
| `test_retrieval_bench.py` | 10 | Golden-set schema, recall/MRR arithmetic, compare deltas and oracle metrics |
| **Total** | **296** | |

Two standalone harnesses run outside `unittest`:

| Harness | Covers |
|---|---|
| `wiki/test_bootstrap.py` | **Greenfield setup** — empty, memory-only and full modes end to end in a temp directory, including FTS over paths, emails and FTS5 operator characters |
| `wiki/test_vault_mcp.py` | MCP protocol conformance over real stdio JSON-RPC |

**A change to trigger definitions, `export_context`, or `search_memory` must be
accompanied by a benchmark run.** The tests prove correctness; only the
benchmark proves you did not reintroduce defect #9.
