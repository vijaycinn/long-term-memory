# Implementation Plan — Retrieval v4

**Target repo:** `vijaycinn/long-term-memory` · working copy `C:\workspace\agency\long-term-memory`
**Written:** 2026-09-02 · **Status:** proposed, not started
**Audience:** an implementing agent. Every task is scoped, testable and reversible.

---

## 0. Why this exists

An audit on 2026-09-02 established the following ground truth about the running
system. All numbers are measured, not estimated.

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

**The thesis:** the graph exists and is never read at query time. Fix the read
path before buying an embedding model. Steps 1–4 add **no new dependency**.

**Non-goals.** Not running full GraphRAG (its published win is global
query-focused summarisation over ~1M-token corpora, `arxiv.org/abs/2404.16130`;
it would LLM-re-extract a graph already curated by hand). Not replacing FTS5.
Not changing the vault's authoring model. Not building a scripted installer —
the portability half of this plan is **documentation only**, by decision.

---

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
4. **Tests must stay green.** 172 unittest methods plus two harnesses:
   ```powershell
   $env:PYTHONIOENCODING = "utf-8"
   python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py" -t tests
   python wiki\test_bootstrap.py    # greenfield bootstrap into a temp dir
   python wiki\test_vault_mcp.py    # MCP protocol conformance
   ```
   No test depends on real data or real paths — every absolute-looking string in
   `tests/` is a *value under test*, not a filesystem target.
5. **One task per commit**, each independently revertable.
6. Uncommitted state at time of writing: `M wiki/vault_mcp.py` (a docstring
   de-hardcoding page counts — harmless), plus untracked
   `hooks/enforce_settings.py` and `hooks/enforced-settings.json`. Commit or
   stash before starting so the diff stays legible.

---

## PHASE 0 — Baseline, or none of this is provable

> **Gate: no task in Phase 1–3 may be merged without a before/after number from
> this harness.** None of the published figures in the research (Anthropic's
> −67%, LazyGraphRAG's 700×) were measured on this corpus. They justify trying;
> they do not predict the result here.

### T0.1 — Build a golden query set

**File:** `tests/golden/queries.json` (new)
**Do:** Extract 25–30 real questions actually asked of this vault. Sources, in
priority order: `~/.copilot/session-store.db` user messages; the vault's
`log.md`; `01-Activity/Sessions/`. Prefer questions whose answers are known.

Each entry:
```json
{ "id": "g01",
  "query": "what did we conclude about SAIF's OCR cost",
  "expect_pages": ["SAIF (TPID 20847298) — Foundry OCR options for the 65M-document OnBase backfill"],
  "expect_facts": [],
  "class": "local|global|multihop|paraphrase" }
```
Deliberately include **8+ `paraphrase`** cases — queries using different
vocabulary from the target page. That is the class today's substring search
returns *zero* on, so it is where the whole argument for Phase 2 lives.

**Acceptance:** ≥25 entries, ≥8 paraphrase, every `expect_pages` target resolves
to a real file.

### T0.2 — Measurement harness

**File:** `ops/retrieval_bench.py` (new)
**Do:** For each golden query, run every configured retrieval path and report
**recall@5**, **MRR** and **p50/p95 latency**, plus a per-class breakdown.
Paths: `vault_search` (current), `memory_search` (current), and each new path
behind its flag. Must accept `--baseline out.json` and `--compare a.json b.json`
and print a delta table.

**Acceptance:** `python ops\retrieval_bench.py --baseline docs\bench\baseline-2026-09-02.json`
runs clean, writes the file, prints a table. Commit that baseline file — it is
the number everything else is measured against.

**Expected baseline shape (prediction, to be checked):** paraphrase recall near
**0** for `vault_search`, because `all(t in low for t in terms)` cannot match
absent tokens. If it is not near zero, stop and re-examine the audit.

---

## PHASE 1 — Free wins. No dependency, no model, no index.

### T1.1 — Traverse the edges that already exist ★ highest leverage

**File:** `wiki/vault_mcp.py` — `t_read` (`:129-142`), `t_search` (`:84-111`)
**Do:**
- Add a `_neighbours(text, limit=8)` helper that parses `[[Target]]` (and
  `[[Target|alias]]`) out of page text, resolves each via the existing
  `_resolve()` (`:113`), and returns `(title, summary)` pairs read from
  frontmatter.
- Add `hops: int = 0` to `t_read`. When `hops >= 1`, append a
  `## Linked pages` block listing neighbour titles + summaries. **Do not inline
  neighbour bodies** — that is how you blow the context window.
- Classify neighbours: links from a `## Related` block are auto-generated
  name-matches (`notes.py:37-58`) and must be labelled as such, separately from
  inline body links, which are stronger signal. Prefer inline when truncating.
- Cap total added text at ~2,000 chars; state the cap in the tool description.

**Why this is the cheapest real win:** this is exactly GraphRAG's Local Search
pattern (neighbour fan-out from a hit), implemented over edges that already
exist and currently contribute nothing.

**Acceptance:** a golden query whose answer lives on a *neighbour* of the
best-matching page resolves at `hops=1` and fails at `hops=0`. Multi-hop class
recall@5 improves. `python wiki\test_vault_mcp.py` still passes.
**Rollback:** `hops` defaults to 0 — behaviour is identical when unset.

### T1.2 — Restore relevance ranking

**File:** `wiki/vault_mcp.py:247-273` (`_fts_query`)
**Do:** Put `bm25(memory_fts) AS rank` back in the SELECT and blend it with
importance instead of letting importance override relevance.

> ⚠️ **Sign trap:** SQLite's `bm25()` returns **negative** values where *more
> negative = better*. `ORDER BY rank` ascending is correct. An agent that sorts
> descending will silently invert relevance and the tests will still pass.
> Normalise before blending; do not add a raw bm25 score to a 1–5 importance.

Suggested: min-max normalise `rank` across the result set to `[0,1]` (1 = best),
then `score = 0.7 * rank_norm + 0.3 * (importance / 5)`. Expose the weights as
module constants so they can be tuned by T0.2 rather than by taste.

**Also fix the divergence:** `core/memory_driver.search_memory` orders by bm25
(`:316`) while this tool orders by importance — so `Search-Memory` in PowerShell
and `memory_search` in chat return **different orderings for the same query**.
After this change, assert they agree.

**Acceptance:** new test `tests/test_vault_mcp_rank.py` — a rare precise term
must outrank a generic importance-5 fact. Add a regression test asserting
PowerShell and MCP orderings match for 5 golden queries.
**Rollback:** single commit, revert cleanly.

### T1.3 — Join `entity_mentions` on a fact hit

**File:** `wiki/vault_mcp.py:247-273`
**Do:** After the FTS hit set is resolved, expand it: for each hit fact, find its
entities, then other facts mentioning the same entities. Return them in a
clearly-labelled `related_facts` block, capped at 5, never displacing direct
hits. Both indexes already exist (`setup/init-memory.sql:235-236`) — this is a
JOIN, not a migration.

**Depends on T1.4** to be useful: today `entity_mentions` has 3 rows and none
point at a fact, so this returns nothing until the backfill lands. Implement in
this order anyway — build the read path, then fill it.

**Acceptance:** with T1.4 applied, a query naming an entity returns co-mentioned
facts from *other* topics. Verify the cap holds and direct hits are never lost.

### T1.4 — Backfill the 251 latent entity edges

**File:** `ops/backfill_entity_mentions.py` (new)
**Do:** For each active fact, match `facts.content` against `entities.name`
(case-insensitive) and insert `entity_mentions(entity_id, parent_id,
parent_type='fact', relationship='mentions')`.

**Reuse the existing matching discipline — do not invent one.** `notes.py:30-35
register()` already drops aliases under 4 characters and anything in
`STOP_ALIASES`. Import and apply the same rule, or short entity names will
produce garbage edges. The `UNIQUE(entity_id, parent_id, parent_type,
relationship)` constraint makes re-runs safe.

**Must be:** `--dry-run` by default, `--apply` to write, consistent with
`ops/ltm_maintain.py`. Print a sample of 20 proposed edges for eyeball review
before any write.

**Acceptance:** dry run reports ≈251 candidate edges (audit figure — if it
differs materially, investigate before applying). After `--apply`, fact-level
coverage moves from **0%** to >25%. Take a backup first: `Backup-Memory`.
**Rollback:** `DELETE FROM entity_mentions WHERE parent_type='fact' AND created_at >= '<stamp>'`.

### T1.5 — Wire the same edges into the session-end writer

**File:** `hooks/ltm_session_end.py:109`
**Do:** The hook already writes `entity_mentions` — confirm it does so for
`parent_type='fact'`, not only `'topic'`. The audit found **0** fact-level rows
despite 819 facts, which suggests it either never fires for facts or fires on a
path not taken. Trace it, fix it, and add a test. Otherwise T1.4's backfill
decays immediately and you re-accumulate latent edges.

**Acceptance:** `tests/test_session_end.py` gains a case asserting a fact
mentioning a known entity produces a fact-level `entity_mentions` row.

### ✅ Phase 1 exit gate

Re-run `ops/retrieval_bench.py --compare baseline.json phase1.json`. Record the
delta in `docs/bench/`. **Phase 2 is not authorised unless Phase 1 measurement
shows where the remaining loss is.** If Phase 1 closes most of the gap, the
embedding work may not be worth doing at all — that is a legitimate outcome.

---

## PHASE 2 — Semantic layer. New infrastructure. Only if Phase 1 justifies it.

### T2.1 — Vector sidecar with `sqlite-vec`

**Files:** `wiki/vault_vectors.py` (new), `wiki/vault_mcp.py:84` (`t_search`)

**Choice rationale — record it, it is not about vector-DB features.** The
measured constraint is that **Python process spawn is ~93% of wall time**
(519 ms vs 39 ms of DB work). Any store needing a second process or a server
loses on startup before retrieving anything. `sqlite-vec` loads as an extension
into the same connection already open. Verified on this machine 2026-09-02:
Python 3.13.14, SQLite 3.50.4, `enable_load_extension: True`.

**Do:**
- Sidecar DB at `config.get("vault_vectors")`, default
  `COPILOT_HOME/vault-vectors.db`. **Never** put vectors in `memory.db` — it
  would bloat the backup/restore path that `ops/ltm_backup.py` guarantees.
- Key each row by `(path, mtime, chunk_ix)` so invalidation reuses the existing
  mtime cache at `vault_mcp.py:54`. Re-embed only changed files.
- Brute-force KNN is correct at this size (~2,500 vectors, sub-ms). Do **not**
  add an ANN index. Revisit around 100k vectors.
- **Pin the version** — `sqlite-vec` is explicitly pre-v1.
- **Degrade gracefully:** wrap import and extension load in try/except. If
  either fails, log once and fall back to today's lexical path. A peer without
  the extension must still get a working vault.

**Chunking — the trap:** `all-MiniLM-L6-v2` truncates at **256 word-pieces**.
The largest vault page is 40,043 words. Embedding whole pages silently discards
almost all of it. Chunk to the model's window with overlap, store the parent
path, and return the **parent page** on a hit (small-to-big). Record the chosen
model's dimension and context length in the config file.

**Cost:** ~2,500 vectors × 384-d int8 ≈ 1 MB. Embedding the corpus at
`text-embedding-3-small` list price ($0.02/1M tokens) is **under one cent** —
cost is not the deciding factor, offline capability is. Prefer a local model so
the vault works with no network.

### T2.2 — Hybrid fusion

**File:** `wiki/vault_mcp.py:84` (`t_search`)
**Do:** Run lexical and vector retrieval in parallel and merge with **Reciprocal
Rank Fusion**: `score(d) = Σ 1/(k + rank_i(d))`, `k = 60`. This is the pattern
Azure AI Search documents as its default relevance story (BM25 + vector + RRF,
optional semantic reranker).

**Important:** upgrade the lexical side at the same time. Today it is
`str.count()`, not BM25. Either build an FTS5 index over vault pages (preferred
— you already depend on FTS5) or implement BM25 in Python. Fusing a *good*
vector score with a *bad* lexical score wastes the vector work.

Expose `mode` on `t_search`: `lexical` (today), `vector`, `hybrid` (default once
proven). Keep `lexical` reachable forever — it is the fallback when the
extension is missing.

**Acceptance:** paraphrase-class recall@5 rises from the baseline (predicted
near zero) to materially non-zero. p50 latency stays under 300 ms. Lexical mode
byte-identical to today's output.

### T2.3 — Contextual retrieval *(optional, last, most expensive)*

**File:** build step in `wiki/vaultlib.py`
Prepend a 50–100 token "where this chunk lives" header to each chunk before
indexing, for **both** lexical and vector paths. Anthropic measured this cutting
top-20 retrieval failures 35%, 49% with contextual BM25, 67% with a reranker —
on *their* corpus. It requires an LLM pass over the corpus at build time, which
breaks the stdlib-only property and adds real build cost.

**Do not start this until T2.2 is measured.** If hybrid already clears the bar,
this is unjustified spend.

---

## PHASE 3 — Stop injecting blind

### T3.1 — Use `initialPrompt` at session start

**File:** `hooks/ltm_session_start.py:293` (`generate_instructions`)
**Do:** The stdin contract already carries `initialPrompt` (documented at `:7`)
and the function never reads it. Today it injects a fixed **23,457 chars
(~6k tokens)** regardless of what was asked, drawn from eight hardcoded
`LIMIT`-bounded queries.

Keep the unconditional core — identity, importance-5 constraints, open todos.
Replace **part** of the fixed 20-fact block (`:110`) with facts retrieved
against `initialPrompt` via `search_memory`. Suggested split: 10 fixed
importance-5, 10 query-relevant. Skip retrieval and keep today's behaviour when
`initialPrompt` is empty or under ~15 characters.

**Watch the budget:** the hook has a 10,000 ms timeout and the DB work is ~39 ms,
so one extra query is affordable — but it runs on **every** session start.
Measure it.

**Acceptance:** a session started with a topic-specific prompt surfaces facts
that the fixed block omits. Total injected size does not grow.
`tests/test_session_start.py` gains cases for empty / short / long `initialPrompt`.

---

## PHASE 4 — Standing this up on a peer's PC *(documentation only)*

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
| **B1** | **Repo is private under the personal `vijaycinn` account.** A peer on a `_microsoft` EMU account gets a misleading `Repository not found`, and an EMU user cannot fork outside the enterprise. No script fixes this. | Move to an org repo, or add the peer as a collaborator. Policy decision, not code. |
| **B2** | `copilot-config/hooks/hook-guard.json` ships **6 absolute `C:/workspace/agency/...` paths**, and line 22 invokes `mcp-oauth-prune.ps1` — **a script not present in this repo**. The documented restore is a raw `Copy-Item`, so a peer registers 4 sessionEnd hooks of which 3 point at nothing. | Generate it from `$RepoRoot` in `install-memory.ps1`; drop the missing script or ship it. |
| **B3** | `setup/seed_memory.py` seeds the **VOF grievance dataset** — named individuals, HR-sensitive claims — behind a flag called `-SeedExampleData`. Opt-in, but the name actively misleads. | Replace with neutral sample data, or rename the flag `-SeedVofData` and document it. **Do not ship personal HR data to a peer.** |
| **B4** | `wiki/build.py:398,402` writes `. C:\workspace\agency\long-term-memory\memory.ps1` and `cd C:\workspace\agency\obsidian\_build` **into the peer's own generated index page**, as the instructions they are told to follow. | Interpolate from `config.paths()` / `Path(__file__)`. Three lines: `:373`, `:398`, `:402`. |

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
  then the 172-test suite. Restart the CLI — MCP servers and hooks load at
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

**File:** `wiki/README.md:166-167` gives `cd C:\workspace\agency\long-term-memory`
then `python -m unittest test_vault_sync -v` — hardcoded *and* wrong relative to
the `-t tests` layout. Replace with the correct command from `README.md:612-617`.

### P4.3 — Document the new surface

Update `README.md` and `CAPABILITIES.md` for anything Phase 1–3 adds: the `hops`
argument, `mode` on `t_search`, the vector sidecar and its optional dependency,
the backfill script, and schema v4 if reached. `CAPABILITIES.md` is the
documented entry point for agents working on this repo — if it is not there, the
next agent will not find it.

---

## Sequencing

```
T0.1 golden set ──► T0.2 harness ──┬─► T1.1 traverse ──┐
                                   ├─► T1.2 bm25 ──────┤
                                   ├─► T1.4 backfill ──┼─► T1.3 join ─┐
                                   └─► T1.5 hook fix ──┘              │
                                                                      ▼
                                                          ══ Phase 1 gate ══
                                                                      │
                                          ┌───────────────────────────┘
                                          ▼
                          T2.1 sqlite-vec ──► T2.2 hybrid ──► T2.3 contextual (optional)
                                          │
                                          └─► T3.1 initialPrompt  (independent, any time after Phase 0)

P4.0–P4.3 are independent of everything above and can run in parallel.
```

T1.1, T1.2, T1.4, T1.5 are mutually independent — parallelise them.
T1.3 requires T1.4 to return anything.

## Definition of done

- [ ] `docs/bench/baseline-2026-09-02.json` committed, and a delta file per phase
- [ ] Paraphrase-class recall measurably improved, with the number recorded
- [ ] 172 tests + both harnesses green
- [ ] `python wiki\test_bootstrap.py` green — the greenfield path still works
- [ ] No new absolute path anywhere; every new setting flows through `config.get()`
- [ ] Vector path degrades to lexical when `sqlite-vec` is absent
- [ ] `docs/INSTALL-PEER.md` followed end-to-end on a clean machine (or a clean
      user profile) by someone who did not write it
- [ ] `README.md` + `CAPABILITIES.md` updated
- [ ] A decision recorded on each P4.0 blocker — fixed, or accepted with reason

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
