# Phase 1 gate — retrieval v4

**Date:** 2026-09-02
**Baseline:** `docs/bench/baseline-2026-09-02.json`
**After Phase 1:** `docs/bench/phase1-2026-09-02.json`
**Verdict: Phase 2 NOT authorised — insufficient evidence, not a negative result.**

---

## What the numbers say

| Path / class | Baseline recall@5 | Phase 1 recall@5 | Δ |
|---|---:|---:|---:|
| `lexical_v0` / all | 0.370 | 0.370 | 0.000 |
| `lexical_v0` / paraphrase | 0.000 | 0.000 | 0.000 |
| `vault_current` / all | 0.370 | 0.370 | 0.000 |
| `vault_current` / local | 0.615 | 0.615 | 0.000 |
| `vault_current` / multihop | 0.667 | 0.667 | 0.000 |
| `vault_current` / paraphrase | 0.000 | 0.000 | 0.000 |
| `memory_v0` / all | 0.000 | 0.000 | 0.000 |
| `memory_current` / all | 0.000 | 0.000 | 0.000 |

Phase 1 moved **nothing** this harness can see.

## Why — and why that is not the same as Phase 1 failing

The harness measures **page-search recall**. Phase 1 did not change page search.

| Task | What it changed | Does the bench exercise it? |
|---|---|---|
| T1.1 link traversal | `t_read(hops=1)` — **read** time | ❌ bench only calls search |
| T1.2 bm25 blend | `memory_search` fact ranking | ❌ see below |
| T1.3 entity join | `memory_search` `related_facts` | ❌ see below |
| T1.4 backfill | 428 fact edges written | ❌ only observable through T1.3 |
| T1.5 hook fix | keeps those edges fresh | ❌ not a retrieval path |

`t_search` — the one function the bench measures — was deliberately left alone,
so `vault_current == lexical_v0` to three decimals is the **expected and correct**
result, and is itself a useful regression check: Phase 1 did not damage the
lexical path.

Both memory paths read 0.000 **by construction**, not by measurement: all 27
golden queries carry `expect_facts: []`, because the set was built page-centric
(T0.1). A path with no expected targets cannot score above zero. **The 0.000 on
`memory_current` is an artifact of the golden set, and must not be read as
"bm25 ranking did not help."**

## The gap to close before Phase 2 is decided

The plan's gate says Phase 2 is unauthorised *"unless Phase 1 measurement shows
where the remaining loss is."* It does not. Two harness extensions are needed
first, both cheap:

1. **Populate `expect_facts`** on the golden queries, or add a fact-centric
   query set. Until then T1.2 and T1.3 — two of the four Phase 1 read-path
   changes — are shipped **unmeasured**.
2. **Add a `vault_current_hops1` path** that resolves the top hit then expands
   via `t_read(hops=1)`. This is the only way the `multihop` class can show
   T1.1's value. Multihop recall is 0.667 today on lexical alone; T1.1 exists
   precisely to lift the remaining third.

## What *is* established

- **The paraphrase failure is real and total.** `lexical_v0` paraphrase
  recall = **0.000** across 9 deliberately vocabulary-divergent queries. The
  audit's model of the failure is confirmed: `all(t in low for t in terms)`
  cannot match absent tokens. This is the strongest argument for a semantic
  layer, and it survives independent of everything above.
- **Phase 1 shipped no regression.** Lexical recall unchanged, 197 tests green,
  greenfield bootstrap green, MCP conformance 9/9.
- **The graph is now readable.** 0 → 428 fact-level entity edges (27.5% of
  active facts), and the read path to consume them exists.

## Recommendation

Close the two measurement gaps, re-run, then decide. If paraphrase recall is
still 0.000 after that — which is likely, since nothing in Phase 1 addresses
vocabulary mismatch — Phase 2 (`sqlite-vec` + RRF hybrid) is justified on that
class alone. Do **not** authorise it on the current numbers, because the current
numbers do not measure what Phase 1 built.

## Latency note

`vault_current` p50 is 140 ms against `lexical_v0` 73 ms. Both are warm-cache and
both sit far under the plan's 300 ms bar. The gap is harness overhead — the
frozen `lexical_v0` is an inline scorer, while `vault_current` goes through
`read_cached()`, frontmatter parsing and tuple construction. `t_search` itself is
functionally unchanged, so this is **not** a Phase 1 regression. Worth a second
look only if it grows.
