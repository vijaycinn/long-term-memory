# Implementation Plan — Retrieval v4

**Target repo:** `vijaycinn/long-term-memory`
**Written:** 2026-09-02 · **Updated:** 2026-09-02
**Status:** In Progress (Phases 0–3 implemented; Phase 2 measured and tuned on the restored index; Phase 4 portability fixes partial)
**Audience:** an implementing agent. Every task is scoped, testable and reversible.

---

## 0. Implementation Status & Audit Summary (as of 2026-09-02)

| Phase / Task | Implementation Status | Test & Benchmark Findings | Next Action / Gate |
|---|---|---|---|
| **Phase 0** (T0.1, T0.2) | **Implemented** (`queries.json`, `retrieval_bench.py`) | 27 golden queries defined. T2.4 fixed padded-marker parsing, populated `expect_facts`, and added `vault_current_hops1`. Valid full-index pre-reranker baseline: Recall@5 **16/27 (59.3%)**, MRR **0.364** overall, measured against **619 pages / 7,940 chunks**. Final artifact is now provenance-bearing at **648 pages / 8,279 chunks**. | Complete |
| **Phase 1** (T1.1–T1.5) | **Implemented** in code (`vault_mcp.py`, `entity_mentions`) | Link traversal (`hops`), FTS BM25 + importance blend, entity backfill, and session-end hook active. T2.5 reconciled CLI/MCP ordering. | Complete |
| **Phase 2** (T2.1–T2.9) | **Implemented and measured** in code (`vault_vectors.py`, `vault_mcp.py`, `vault_rerank.py`) | Final chosen `RELAXED_RRF_K = 120` on **648 pages / 8,279 chunks**: overall Recall@5 **0.667** and MRR **0.472**. g04 is back in the pool at rank **18** but remains outside top 10; g26 is rank **10**. K=120 is a qualified candidate-pool improvement, not a clean metric win. | Complete measurement; tune only with broader evaluation; T2.3 remains **GO** |
| **Phase 3** (T3.1) | **Implemented** in code (`hooks/ltm_session_start.py`) | `initialPrompt` prompt-aware fact injection into `memory-context.md`. Does not alter vault-page search metrics. | Complete |

**The core diagnostic insight:** Dense embeddings + RRF hybrid fusion cured the structurally-zero vocabulary failure, but raw BM25 still had a long-query recall bug until T2.9. The selected relaxed-OR displacement repair improves candidate-pool coverage without improving Recall@5: at K=120, MRR is **0.472** versus **0.488** at K=60, while g04 returns at pool rank 18 and g26 reaches rank 10. This is a qualified structural repair, not a net metric win.

---

## 0.1 Why this exists

At the start of the 2026-09-02 audit, the following measurements described the
running system. They are historical baseline observations; later rows in this
plan record the implemented changes. All numbers are measured, not estimated.

| Finding | Measured | Evidence |
|---|---|---|
| No embeddings, cosine or vector index exist anywhere | — | grep across `long-term-memory/**` |
| `vault_search` is exact substring AND, scored by `str.count()` | 1,574 ms median | `wiki/vault_mcp.py:92-97` |
| `memory.db` FTS5 MATCH, warm | 0.08 ms | `core/memory_driver.py:313` |
| The MCP tool **discards** bm25 and orders by `importance DESC` | — | `wiki/vault_mcp.py:264` |
| Vault wikilinks | 6,342 across 619 pages | filesystem scan |
| …of which boilerplate (`## Related` + breadcrumb) | **61%** | 2,628 + 1,242 |
| Pages with zero inline outbound links | **528 / 619 (85.3%)** | — |
| **No retrieval path parses `[[...]]`** | — | `vault_mcp.py:84-111`, `:129-142` |
| Facts carrying a structured entity edge | **0 of 819** | `entity_mentions` has 3 rows, none `parent_type='fact'` |
| Facts naming a known entity with no edge recorded | **251 (30.6%)** | latent edges |
| Session-start injection reach | 9,845 tokens of ~1,233,000 = **0.8%** | `memory-context.md` vs vault total |
| `initialPrompt` arrives on stdin and is discarded | — | `hooks/ltm_session_start.py:293` |

**The thesis:** the graph exists and is never read at query time. Fix the read path before buying an embedding model. The Phase 1 read-path fixes add
**no new dependency**; Phase 2 later introduced an optional vector dependency.

**Non-goals.** Not running full GraphRAG (its published win is global
query-focused summarisation over ~1M-token corpora, `arxiv.org/abs/2404.16130`;
it would LLM-re-extract a graph already curated by hand). Not replacing FTS5.
Not changing the vault's authoring model. Not building a scripted installer —
the portability half of this plan is **documentation only**, by decision.

---

## 0.2 Corrections to this plan (2026-09-02)

1. **T2.6 was a false positive.** `wiki/vault_embed.py` uses the `token` variable for the bearer header and contains no literal asterisk placeholder. The apparent `Authorization: ******` came from display-time secret redaction in tool output. A novel, never-before-embedded query returned a real 512-dimensional vector; `git diff wiki/vault_embed.py` is empty. Only a regression test was added. **Do not treat the redacted display string as source content; anyone grepping the file will see the same trap.**
2. **The headline metrics were parser error.** The live renderer right-aligns markers with `{marker:>10}`, so result lines begin with spaces. `_current_vault()` tested `line.startswith("[")` and reported false zeroes for `vault_current`. After T2.4, the measured pre-reranker baseline against the healthy **619-page / 7,940-chunk** index was Recall@5 **16/27 (59.3%)** and MRR **0.364** overall; the earlier paraphrase **0.182**, GCC **4/6 (66.7%)**, and CU **3/5 (60.0%)** headline comparison was not a valid baseline.
3. **BM25 had a recall bug the plan did not diagnose.** `search_bm25` passed long natural-language text directly to FTS5, whose implicit AND required every term in one chunk and often returned zero rows. The AND-then-OR cascade in T2.9 restored two-retriever fusion and raised paraphrase Recall@5 from **2/11 (18.2%)** to **3/11 (27.3%)**. The failure was not primarily a reranking problem.
4. **The original T2.7 target was unreachable by reranking alone.** The protected T2.8 artifact reports paraphrase Recall@20 **5/11 (45.5%)**; only five targets enter the candidate pool, so a perfect reranker caps at **5/11**, below the requested **>0.500**. Overall top-20 Recall is **21/27 (77.8%)**, with six targets outside the pool: `g03`, `g04`, `g17`, `g19`, `g22`, `g27`. T2.3 contextual chunking is therefore **GO**, with the small-sample caveat that one query changes the overall rate by **3.7 percentage points**.

**Artifact integrity note:** The protected oracle was read at **21/27** before a concurrent process rewrote the same path to a later **22/27** measurement. This closeout did not write that file. A settled read-only rerun reproduced **22/27**, but the corpus/index had changed during the session; the 21/27 snapshot remains the T2.8 reference for the ceiling claims above. Reconcile the artifact history before treating the later file as comparable.
The earlier closeout artifact at `docs/bench/retrieval-v4-final-2026-09-02.json` was written at **20:57Z** and **21:27Z** without sidecar-count provenance, then deleted as invalid after the partial-index discovery. A fresh artifact was written at **21:36Z** after restoration and carries **8,279 chunks / 648 pages** provenance.

5. **The g04/g26 regression was repaired with a qualified trade-off.** `wiki/vault_vectors.py` now tags BM25 hits as `strict` or `relaxed` and fuses relaxed-OR contributions with a separate `RELAXED_RRF_K`. The sensitivity sweep on **648 pages / 8,279 chunks** chose **K=120**, the lowest intervention above `RRF_K = 60` that returns g04 to the candidate pool at rank **18** and g26 to the top 10 at rank **10** while preserving Recall@5. Overall Recall@5 is **0.667** versus **0.667** at K=60, while MRR is **0.472** versus **0.488**; g02 is rank **3**, g01 rank **1**, and g21 rank **4**. K=180/240 lose Recall@5; K=480 lowers MRR to **0.447**. Held-out support is weak: g17 and g20 did not improve, and g04/g26 motivated the repair. This is not a clean metric win; tuning against 27 queries risks overfitting.
6. **The sidecar truncation root cause is an index-CLI defect.** `python wiki\vault_vectors.py index --limit-pages` rebuilds from scratch rather than incrementally, so a limited run wipes the full shared index. That caused the drop from 619 pages to 33; the sidecar later self-healed to the restored 648-page state. Treat `--limit-pages` as destructive until the CLI is fixed.
7. **`Start-Sleep` is not a valid sub-agent wait primitive.** It can fail with `Permission denied and could not request permission from user`; do not build sleep-poll loops for shared state. Check once and proceed, or report blocked.

The reranker measurement was mixed, not a clean win: local (n=13) Recall@5 / Recall@3 / MRR **0.692/0.615/0.528 → 0.846/0.846/0.723**; paraphrase (n=11) **0.182/0.182/0.159 → unchanged**; multihop (n=3) **1.000/1.000/0.333 → 1.000/1.000/0.500**; latency p50/p95 **90.3/192.2 ms → 137.2/275.1 ms**. Litmus moves were g06 **7→2** and g13 **4→1**; g12 stayed at 15 and g03 was absent from the pool. Separately, g12 may be a golden-set defect: its top hit states that M365 GCC customers use commercial Azure, while the golden expectation is `Microsoft Fabric`.

## 1. Constraints the implementing agent must honour

1. **Never hardcode a path.** `wiki/config.py` already resolves everything:
   explicit arg → env var → `~/.copilot/wiki-config.json` → default
   (`config.py:14-15`, `:50-61`). Env vars: `COPILOT_HOME`, `LTM_WIKI_VAULT`,
   `LTM_MEMORY_DB`, `LTM_SESSION_DB`, `LTM_WIKI_WORKSPACE`. All new code calls
   `config.get()`. Any new absolute path is an automatic review rejection.
2. **Stdlib only, by design.** The entire repo has exactly one third-party
   import — `pyyaml`, guarded by `try/except ImportError`
   (`wiki/check_frontmatter.py:16-19`). Python floor is **3.10+**
   (`README.md:93`). Phase 2 is the first proposal to break this; it must stay
   optional and degrade to today's behaviour when absent.
3. **Schema changes go through `migrate()`**, never `ALTER TABLE`. The schema is
   all `CREATE TABLE IF NOT EXISTS` with a `schema_version` table currently at
   **v3** (`setup/init-memory.sql:11-12`); the upgrade path is
   `core/memory_driver.py:548 migrate()`. Bump to v4 and extend `migrate()`.
4. **Tests must stay green.** 258 unittest methods plus two harnesses:
   ```powershell
   $env:PYTHONIOENCODING = "utf-8"
   python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py" -t tests
   python wiki\test_bootstrap.py    # greenfield bootstrap into a temp dir
   python wiki\test_vault_mcp.py    # MCP protocol conformance
   ```
   No test depends on real data or real paths — every absolute-looking string in
   `tests/` is a *value under test*, not a filesystem target.
5. **One task per commit**, each independently revertable.
6. The working tree may contain concurrent agent changes. Record the revision and
   `git diff --stat` with every benchmark; do not call a torn measurement stable.

---

## PHASE 0 — Baseline & Measurement Harness [Implemented]

> **Status:** Implemented (`tests/golden/queries.json`, `ops/retrieval_bench.py`). T2.4 adapter and metric-completeness fixes are complete; a fresh full-index final artifact is pending sidecar restoration.

### T0.1 — Build a golden query set [Implemented]

**File:** `tests/golden/queries.json`
**Status:** Implemented (27 golden queries: 13 local, 11 paraphrase, 3 multihop).
**Do:** Extract 25–30 real questions actually asked of this vault. Sources, in
priority order: `~/.copilot/session-store.db` user messages; the vault's
`log.md`; `01-Activity/Sessions/`. Prefer questions whose answers are known.

### T0.2 — Measurement harness [Implemented]

**File:** `ops/retrieval_bench.py`
**Status:** Implemented CLI supporting `--baseline`, `--compare`, and `--oracle`. T2.4 fixed padded-marker parsing, populated fact expectations, and added `vault_current_hops1`.

---

## PHASE 1 — Free wins. No dependency, no model, no index. [Implemented]

> **Status:** Code implemented (`wiki/vault_mcp.py`, `core/memory_driver.py`, `ops/backfill_entity_mentions.py`, `hooks/ltm_session_end.py`). 258 unittests pass; bootstrap and MCP protocol harnesses are green.

### T1.1 — Traverse the edges that already exist ★ highest leverage [Implemented]

**File:** `wiki/vault_mcp.py` — `t_read`, `t_search`
**Status:** Implemented (`_neighbours()`, `hops` parameter in `t_read`).
**Acceptance:** `hops=1` appends `## Linked pages` block with neighbor titles and summaries. Tested in `tests/test_vault_mcp.py`.

### T1.2 — Restore relevance ranking [Implemented]

**File:** `wiki/vault_mcp.py:247-273` (`_fts_query`)
**Status:** Implemented in both callers with the shared `score_fts_hit` function. The CLI and MCP rankings were verified identical on the regression set.

### T1.3 — Join `entity_mentions` on a fact hit [Implemented]

**File:** `wiki/vault_mcp.py:247-273`
**Status:** Implemented (`related_facts` block returns up to 5 co-mentioned entity facts).

### T1.4 — Backfill latent entity edges [Implemented]

**File:** `ops/backfill_entity_mentions.py`
**Status:** Implemented (`ops/backfill_entity_mentions.py --dry-run` and `--apply`).

### T1.5 — Wire edges into session-end writer [Implemented]

**File:** `hooks/ltm_session_end.py`
**Status:** Implemented and tested in `tests/test_session_end.py`.

### ✅ Phase 1 Gate Status

**Finding:** Gate closed after T2.4 populated `expect_facts`, added `hops=1`, and repaired the adapter.

---

## PHASE 2 — Semantic Layer & High-Value Precision Fixes [Measured; K selection complete]

> **Status:** T2.1, T2.2, T2.4, T2.5, T2.6, T2.7, T2.8, and T2.9 are complete. The corrected pre-reranker baseline was Recall@5 **16/27 (59.3%)** and MRR **0.364** overall; T2.9 raised paraphrase Recall@5 to **3/11 (27.3%)**. T2.8 bounds paraphrase Recall@20 at **5/11 (45.5%)**.
> **Known issue:** The K=120 relaxed-OR displacement repair returns g04 to pool rank **18** and g26 to top-10 rank **10**, while oracle Recall@20 is **22/27**. Cost: overall Recall@5 stays **0.667**, MRR declines **0.488→0.472**, and g02 is rank **3**, g01 rank **1**, g21 rank **4**. K=120 is the least intervention above 60, but this is not a clean 27-query metric win.

### T2.1 — Vector sidecar with `sqlite-vec` [Implemented]

**Files:** `wiki/vault_vectors.py`, `wiki/vault_mcp.py`
**Status:** Implemented. Sidecar DB at `~/.copilot/vault-vectors.db`, chunking (~1,200 chars / 200 overlap), content-hash invalidation, KNN retrieval.

### T2.2 — Hybrid fusion with RRF [Implemented]

**Files:** `wiki/vault_mcp.py`, `wiki/vault_vectors.py`
**Status:** Implemented. Sidecar BM25 + dense vectors fused via Reciprocal Rank Fusion (`RRF_K = 60`). Modes exposed: `hybrid` (default), `vector`, `bm25`, `lexical`.

---

### 🔥 PROMOTED HIGH-VALUE PHASE 2 ITEMS (Immediate Implementation)

### T2.4 — Fix Benchmark Adapter Parsing & Metric Completeness (P0 — Blocker) ★

**Files:** `ops/retrieval_bench.py`, `tests/golden/queries.json`
**Status:** Implemented.
**Why:**
1. `_current_vault()` line-matching bug falsely dropped padded `[  hybrid]` lines to `0.000`.
2. All 27 golden queries have empty `expect_facts: []`, making fact-level ranking unmeasurable.
3. No `hops=1` strategy exists in the benchmark to prove graph traversal value.
**Do:**
- Fix `ops/retrieval_bench.py:116-124` regex to parse any `[<mode>]` prefix regardless of leading whitespace.
- Add `vault_current_hops1` strategy to the benchmark runner.
- Populate `expect_facts` in `tests/golden/queries.json` for all 27 queries.
**Acceptance:** `python ops/retrieval_bench.py` reports non-zero, accurate Recall@5 and MRR for `vault_current` matching live MCP output. Met. Final artifact `docs/bench/retrieval-v4-final-2026-09-02.json` carries mandatory provenance: **8,279 chunks / 648 pages**, 512 dimensions, `text-embedding-3-small`. Its warm result is `vault_current` **18/27 (66.7%)**, MRR **0.4722**, p50/p95 **128.06/176.23 ms**; `vault_current_hops1` **17/27 (63.0%)**, MRR **0.4630**, p50/p95 **116.69/168.84 ms**.

### T2.5 — Reconcile PowerShell `Search-Memory` and MCP Scoring (P1)

**Files:** `core/memory_driver.py:316`, `wiki/vault_mcp.py:264`
**Status:** Implemented.
**Why:** PowerShell `Search-Memory` returns pure BM25 ordering while chat `memory_search` returns 0.7 BM25 + 0.3 importance. The same query gives different results depending on caller.
**Do:**
- Export a shared `score_fts_hit(bm25_rank, importance)` function in `core/memory_driver.py`.
- Call the shared scoring function in both `core/memory_driver.py` and `wiki/vault_mcp.py`.
**Acceptance:** Regression test in `tests/test_vault_mcp_rank.py` asserts identical ordering between `Search-Memory` and `memory_search` on 5 test queries.

The original T2.6 title below is retained only to make the display-redaction trap searchable; its current status is a resolved false positive.

### T2.6 — Fix Bearer Token Auth in `vault_embed.py` (P1)

**File:** `wiki/vault_embed.py:120-136`
**Status:** Resolved as a false positive. The source uses the live `token` variable; the asterisks were tool-output secret redaction, not file content. A novel, never-before-embedded query produced a real 512-dimensional embedding, and `git diff wiki/vault_embed.py` is empty.
**Acceptance:** Regression test protects header formatting; no source fix was required.

### T2.7 — Second-Stage Cross-Encoder / Reranker over Fused Candidates (P0 — Highest Metric Leverage) ★★★

**Files:** `wiki/vault_rerank.py` (new), `wiki/vault_mcp.py` (`t_search`)
**Status:** Implemented as a stdlib page-level reranker. It improves several rank-2–6 cases, but it cannot recover pages absent from the fused candidate pool.
**Historical rationale (superseded):** The original cross-encoder diagnosis overstated the problem; T2.9 exposed a separate BM25 recall defect, and T2.8 bounds the reranker ceiling.
The later BM25 displacement repair tags hits as `strict` or `relaxed` and fuses relaxed-OR evidence with `RELAXED_RRF_K = 120` against `RRF_K = 60`. On the restored **648-page / 8,279-chunk** index, it returns g04 to pool rank **18** and g26 to top-10 rank **10**, while overall Recall@5 stays **0.667** and MRR is **0.472** versus **0.488** at K=60. This repaired structural candidate displacement, not the net golden-set metric.
**Why:** This is the direct cure for the 9/11 paraphrase, 4/6 GCC, and 4/5 CU near-misses. Hybrid RRF generates a high-recall top-20 candidate pool; a cross-encoder evaluates `(query, parent_title + summary + chunk)` pairs to demote generic work logs and promote the exact canonical target to rank 1.
**Do:**
- Apply the page-level rerank to the fused candidate pool and preserve the existing hybrid order when a candidate is not improved.
**Measured result:** local (n=13) Recall@5 / Recall@3 / MRR **0.692/0.615/0.528 → 0.846/0.846/0.723**; paraphrase (n=11) **0.182/0.182/0.159 → unchanged**; multihop (n=3) **1.000/1.000/0.333 → 1.000/1.000/0.500**. Held-out queries were unchanged. Latency p50/p95 rose **90.3/192.2 ms → 137.2/275.1 ms**. Litmus rank moves: g06 **7→2** and g13 **4→1**; g12 stayed at 15 and g03 was absent from the pool.
**Bound:** The T2.8 oracle finds only **5/11 (45.5%)** paraphrase targets in the top-20 pool, so the original `>0.500` paraphrase Recall@5 target was mathematically unreachable by reranking alone.

### T2.8 — Candidate-Pool Oracle Measurement Harness (P0)

**File:** `ops/retrieval_bench.py`
**Status:** Implemented and measured earlier against the healthy full index (**619 pages / 7,940 chunks**). The valid reference measurement reported overall Recall@5 **16/27 (59.3%)**, Recall@10 **18/27 (66.7%)**, Recall@20 **21/27 (77.8%)**, and reranker headroom **5/27 (+18.5 pp)**. Six targets were outside the top-20 pool.
The protected artifact was later rewritten concurrently to a different **22/27** result on the restored **648-page / 8,279-chunk** index; this closeout did not overwrite it. The final baseline artifact now carries the restored-index provenance.
**Why:** Proves whether the top-20 fused candidate pool actually contains the expected target before tuning rerankers.
**Do:**
- Add `--oracle` flag to `ops/retrieval_bench.py`.
- Measures **Recall@20** across all golden and litmus queries.
- If Recall@20 is 100%, reranking is guaranteed to achieve perfect retrieval; if <100%, identifies remaining vocabulary gaps.
**Acceptance:** `--oracle` reports the candidate ceiling and identifies the six remaining top-20 misses. Met for the earlier full-index reference; the protected artifact remains unmodified and is the provenance exception.

### T2.9 — Restore BM25 recall for long natural-language queries (P0 — completed)

**File:** `wiki/vault_vectors.py`
**Status:** Implemented.
**Why:** Raw FTS5 MATCH applies implicit AND. Long paraphrase queries therefore required every query term in one chunk and returned zero BM25 hits, silently reducing `search_hybrid` to vector-only.
**Do:** Use an AND-then-OR cascade: retain the precise query when it returns rows, then retry a relaxed OR query when it does not.
**Measured result:** Paraphrase Recall@5 rose from **2/11 (18.2%)** to **3/11 (27.3%)**, restoring real two-retriever fusion. The relaxed-OR displacement repair recovered g04 **absent→rank 5** and g26 **absent→rank 7**, and improved oracle Recall@20 **21/27→22/27**. Cost: overall Recall@5 remained **0.667**, while MRR declined **0.488→0.447**; g02 **2→7**, g01 **1→2**, and g21 **2→5**. This is not a clean metric win.
The **2/11→3/11** measurement was taken against the earlier healthy **619-page / 7,940-chunk** index. The final K=120 sweep was measured against **648 pages / 8,279 chunks**.
**A/B detail:** GCC Hit@1 / Hit@3 / MRR **1/3, 1/3, 0.417 → 2/3, 2/3, 0.667**; CU Hit@3 / Hit@5 / MRR **2/6, 2/6, 0.250 → 3/6, 3/6, 0.333**. The final independent litmus reports combined Hit@1 **2/9→3/9**, Hit@3 **3/9→5/9**, Hit@5 **4/9→5/9**, and MRR **0.306→0.426**. Improvements: g13 **4→1**, g01 **2→1**, g02 **7→3**, g06 **7→2**. g04 remains the unresolved regression (**1→miss**, pool rank 18); g26 is rank **10** versus **7** before. `RELAXED_RRF_K = 120` is the selected least-intervention value; tuning against 27 queries risks overfitting.

**Sensitivity sweep (648 pages / 8,279 chunks):**

| `RELAXED_RRF_K` | Overall Recall@5 / MRR | g01 | g02 | g04 top-10 | g04 pool-20 | g21 | g26 top-10 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 60 | 0.667 / 0.488 | 1 | 2 | — | — | 2 | — |
| **120** | **0.667 / 0.472** | **1** | **3** | — | **18** | **4** | **10** |
| 180 | 0.630 / 0.458 | 1 | 6 | 10 | 10 | 5 | 8 |
| 240 | 0.630 / 0.458 | 1 | 6 | 9 | 9 | 5 | 7 |
| 480 | 0.667 / 0.447 | 2 | 7 | 5 | 5 | 5 | 7 |

K=120 is the lowest intervention above 60 that restores candidate-pool coverage for g04 and top-10 coverage for g26 while preserving Recall@5. It is selected on principled least-intervention grounds, not by maximizing the metric. Held-out support is weak: g17 and g20 did not improve, and g04/g26 motivated the repair.

### T2.3 — Contextual Chunking (P2 — Conditional)

**File:** `wiki/vaultlib.py`
**Why:** If T2.8 shows specific queries missing from the top-20 candidate pool altogether (e.g. CU token billing), prepend 50–100 token contextual headers to chunks during indexing to expand initial candidate recall.
**Status:** **GO**, based on T2.8's six top-20 misses, with a small-sample caveat: one query changes the overall rate by **3.7 percentage points**. Not implemented in this closeout.

---

## PHASE 3 — Stop Injecting Blind [Implemented]

> **Status:** Implemented in `hooks/ltm_session_start.py` (T3.1); covered by the
> current 258-test suite.
> **Scope Clarification:** Phase 3 modifies prompt-aware fact injection into `memory-context.md` at session initialization. It does **not** alter `vault_search` page retrieval or ranking.

### T3.1 — Use `initialPrompt` at session start [Implemented]

**File:** `hooks/ltm_session_start.py:293` (`generate_instructions`)
**Status:** Implemented. Inspects `initialPrompt` on stdin, retrieves relevant facts from `memory.db` via FTS5 for prompts ≥15 chars, blends 10 fixed + 10 prompt-relevant facts, keeps within ~23k char budget. Tested in `tests/test_session_start.py`.

---

## PHASE 4 — Standing this up on a peer's PC *(documentation plus minimal portability fixes)*

The audit found this is **~85% installable today** — better than assumed.
`setup/install-memory.ps1` and `setup/install-wiki.ps1` already create the DB,
self-register both session hooks and the `vault` MCP server, and `config.py` is
fully parameterised with no machine-specific defaults. `wiki/test_bootstrap.py`
is a genuine greenfield proof that runs entirely in a temp directory.

> **Scope note.** Documentation-only was the chosen scope. Four of the items
> below **cannot be solved by documentation** — they are small code/config
> defects that would break or embarrass a peer regardless of what the README
> says. They are listed as P4.0 with their minimal fix. Decide explicitly
> whether to include them; do not let a README paper over them.

### P4.0 — Four things docs cannot fix

| # | Problem | Minimal fix |
|---|---|---|
| **B1** | **Open:** repo is private under the personal `vijaycinn` account. A peer on a `_microsoft` EMU account gets a misleading `Repository not found`, and an EMU user cannot fork outside the enterprise. No script fixes this. | Move to an org repo, or add the peer as a collaborator. Policy decision, not code. |
| **B2** | **Resolved:** `copilot-config/hooks/hook-guard.json` had absolute paths and referenced a missing script, so a peer could register dead hooks. | Generate it from `$RepoRoot` in `install-memory.ps1`; remove the missing reference. |
| **B3** | **Open:** `setup/seed_memory.py` seeds the **VOF grievance dataset** — named individuals, HR-sensitive claims — behind a flag called `-SeedExampleData`. Opt-in, but the name actively misleads. | Replace with neutral sample data, or rename the flag `-SeedVofData` and document it. **Do not ship personal HR data to a peer.** |
| **B4** | **Resolved:** `wiki/build.py` wrote machine-specific instructions into the peer's generated index page. | Interpolate from `config.paths()` / `Path(__file__)`. |

### P4.1 — Peer install guide

**File:** `docs/INSTALL-PEER.md` (new)
Write it as a linear checklist a peer follows without asking questions. It must
cover, grounded in the audit:

- **Preconditions:** Windows, PowerShell, git, **Python 3.10+**. Nothing to
  `pip install` — the repo is stdlib-only apart from optional `pyyaml`
  (`wiki/check_frontmatter.py`). State this explicitly; it is the repo's best
  property and nobody believes it without being told.
- **The EMU trap, up front, not in troubleshooting.** `gh auth switch --user vijaycinn`
  before cloning. GitHub masks 403 as 404, so the failure reads as
  "Repository not found" and sends people down the wrong path.
- **Choose your paths before installing.** Document all five env vars
  (`COPILOT_HOME`, `LTM_WIKI_VAULT`, `LTM_MEMORY_DB`, `LTM_SESSION_DB`,
  `LTM_WIKI_WORKSPACE`) and the `~/.copilot/wiki-config.json` fallback. Default
  vault is `~/ltm-vault`.
- **Run the installers**, with what each writes:
  `install-memory.ps1` → DB + both hooks in `settings.json` + instruction block;
  `install-wiki.ps1` → vault + MCP server under `mcpServers.vault` (stdio).
  Show the exact JSON both produce so a peer can verify or hand-repair.
- **Set your identity — nothing prompts for this.** Without `Set-Identity` the
  context bridge silently renders no identity section and the peer never learns
  why. This is the single most likely "it doesn't work" report.
- **Verify:** `python wiki\test_bootstrap.py`, `python wiki\test_vault_mcp.py`,
  then the 258-test suite. Restart the CLI — MCP servers and hooks load at
  session start only.
- **Upgrading an existing DB:** run `migrate()`. **No installer calls it
  automatically** (`core/memory_driver.py:548`). Say so.
- **Known divergence, stated honestly:** `install-memory.ps1:171-176` registers
  a `sessionStart` hook, while `copilot-config/README.md:78-82` says *"Keep
  `sessionStart` empty"* for startup latency and the maintainer's own live
  config has `[]`. A peer gets a configuration the author rejected. Document
  the trade-off and let them choose.
- **Dead weight:** `hooks/enforce_settings.py` is referenced by no config
  anywhere — say it is optional, or delete it.

### P4.2 — Fix the stale test command

**File:** `wiki/README.md:166-167` gave a machine-specific `cd` followed by
`python -m unittest test_vault_sync -v` — hardcoded *and* wrong relative to the
`-t tests` layout. Replace with the correct repository-relative command from the
main README.
**Status:** Implemented. The stale command was replaced with the repository-relative
test invocation.

### P4.3 — Document the new surface

Update `README.md` and `CAPABILITIES.md` for anything Phase 1–3 adds: the `hops`
argument, `mode` on `t_search`, the vector sidecar and its optional dependency,
the backfill script, and schema v4 if reached. `CAPABILITIES.md` is the
documented entry point for agents working on this repo — if it is not there, the
next agent will not find it. **Status:** Implemented in both documents, including
the reranker, vector modes, and `hops=1` surface.

---

## Sequencing

```
[Phase 0] T0.1 Golden Set ──► T0.2 Harness ──┬─► [Phase 1] T1.1 Graph Traverse ──┐
                                             ├─►           T1.2 BM25 Rank ───────┤
                                             ├─►           T1.4 Backfill ────────┼─► T1.3 Join ─┐
                                             └─►           T1.5 Hook Fix ────────┘              │
                                                                                                ▼
                                                                                  ══ Phase 1 Gate ══
                                                                                                │
                                          ┌─────────────────────────────────────────────────────┘
                                          ▼
[Phase 2] T2.1 sqlite-vec ──► T2.2 Hybrid RRF ──┬─► T2.4 Fix Adapter & Metrics (P0) ──┐
                                                ├─► T2.5 Reconcile CLI/MCP (P1)       │
                                                ├─► T2.6 Resolve Auth Finding (P1)    │
                                                ├─► T2.9 BM25 AND→OR Recall Fix (P0)  │
                                                └─► T2.8 Oracle Ceilings (P0) ────────┼─► T2.7 Page-Level Reranker (P0)
                                                                                      │        (bounded by candidate pool)
                                                                                      │
                                                                                      └─► T2.3 Contextual Chunks (P2, GO)

[Phase 3] T3.1 initialPrompt Fact Injection (Implemented in parallel, independent of search)
[Phase 4] P4.0–P4.3 Peer Installation & Documentation (Independent, can run in parallel)
```

## Definition of Done

- [x] Phase 0 golden query set created — **rebuilt 2026-09-02 to 57 queries** (36 session-provenanced from 3,223 real turns + 21 owner-curated, honestly labelled). The original 27-query set's `paraphrase` class was **synthetic**: zero occurrences of any of its phrasings in `session-store.db` over 180 days. Provenance now lives in `tests/golden/provenance.json` and is enforced by a test, so the defect cannot silently return.
- [x] Phase 1 graph traversal (`hops`), entity backfill, and session-end hooks implemented — note `hops=1` measurably did **not** improve recall and is an exploration aid, not a ranking win
- [x] Phase 2 vector sidecar (`sqlite-vec`), chunking, sidecar BM25, and hybrid RRF implemented
- [x] Phase 3 prompt-aware `initialPrompt` fact injection implemented in `hooks/ltm_session_start.py` — **implemented but not active**: `ltm_session_start.py` is not registered in the maintainer's live `settings.json`
- [x] **281** unittest methods + bootstrap + MCP protocol tests passing green
- [x] **T2.4:** `ops/retrieval_bench.py` adapter bug fixed (regex handles leading whitespace / padded markers)
- [x] **T2.4:** `expect_facts` populated in golden set and `hops=1` benchmark strategy added
- [x] **T2.5:** PowerShell `Search-Memory` and MCP `memory_search` rankings verified identical
- [x] **T2.6:** Resolved as a **false positive**, not a fix. The apparent placeholder in the 
- [x] **T2.7:** Stdlib page-level reranker implemented and measured. Large win where page titles carry the query's vocabulary (local recall@5 0.692 → 0.846); no effect on paraphrase, which its 0.08 semantic weight predicts. A cross-encoder was deliberately **not** built — it would violate the stdlib-only constraint.
- [x] **T2.8:** Candidate-pool Oracle (`--oracle`) implemented. Final: **51/57 (89.5%)** on the corrected set at **8,285 chunks / 651 pages** — local 25/25, paraphrase 21/27, multihop 5/5. Six genuine top-20 misses remain: `g04, g17, g26, g27, g31, g33`.
- [x] **T2.9 / T2.9b:** BM25 AND-then-OR cascade restores long-query recall; `RELAXED_RRF_K = 120` chosen by sensitivity sweep as the lowest intervention that keeps `g04` in the candidate pool.
- [x] **T2.3:** Contextual chunking implemented and **promoted to the live index**. Judged on the synthetic set it rescued 0/5 pool misses and looked worthless; on a 15-query real GCC/Content-Understanding litmus it delivered Hit@1 7/15 → 10/15, Hit@3 9/15 → 12/15, MRR 0.56 → 0.75, **seven improved and zero regressed**. The instrument, not the change, was the problem.
- [x] Paraphrase recall@5 **>0.500** — **MET at 0.630** on the corrected 57-query set (same index, 8,285/651). Read this honestly: the target was originally set against the synthetic set, where the same code scored 0.182. Most of the movement came from fixing the benchmark, not the retriever.
- [x] Fresh end-to-end benchmark artifact recorded with mandatory index provenance
- [x] Fresh oracle artifact recorded with mandatory **pages/chunks** provenance
- [x] **Sync-Vault auto-index** closes the stage-3 gap: captured facts previously never reached the vector index and were semantically invisible until a manual reindex. Verified end to end — `Remember-This` → `Sync-Vault` → vector hit at rank 1 in ~37s. `-NoIndex` opts out.
- [x] **P4.0-B2/B4:** Portability defects fixed without machine-specific paths in generated instructions
- [x] **P4.2:** Stale test command corrected
- [ ] `docs/INSTALL-PEER.md` checklist verified **on a clean machine** — guide written; clean-machine verification still outstanding and cannot be done from this host
- [x] `README.md` and `CAPABILITIES.md` updated with new reranker and vector flags

## What would make me abandon this plan

- Phase 1 closes the gap on its own → **stop**. Do not build Phase 2 for tidiness.
- Baseline shows paraphrase recall is *already* acceptable → the audit's model of
  the failure is wrong; re-audit before writing code.
- `sqlite-vec` proves unstable at pre-v1 → keep hybrid, swap the store, do not
  abandon the read-path fixes, which are independent of it.

## Sources

Local measurements taken 2026-09-02 against `memory.db`, `obsidian\VJ-Agency`,
`wiki/vault_mcp.py`, `wiki/notes.py`, and the repo working copy. External:
cosine similarity on learned embeddings can be arbitrary or non-unique —
`arxiv.org/abs/2403.05440` (measured); GraphRAG targets global QFS over ~1M-token
corpora — `arxiv.org/abs/2404.16130` (authors' bench); LazyGraphRAG index cost
0.1% of GraphRAG, >700× cheaper queries — MSR blog (**vendor, LLM-judged,
synthetic queries**); hybrid BM25 + vector + RRF —
`learn.microsoft.com/azure/search/hybrid-search-overview` (vendor, **no published
nDCG numbers — do not quote any**); contextual retrieval −35%/−49%/−67% top-20
failures — `anthropic.com/engineering/contextual-retrieval` (measured, their
corpus); `sqlite-vec` pre-v1 — `github.com/asg017/sqlite-vec`; FTS5 `bm25()` —
`sqlite.org/fts5.html`.
