# Phase 2 gate — retrieval v4

**Date:** 2026-09-02
**Baseline:** `docs/bench/baseline-2026-09-02.json` (lexical, 2026-09-02)
**Verdict: hybrid search ships as the default. The paraphrase problem is improved, not solved.**

---

## Result

27 golden queries, recall@5 (at least one expected page in the top five):

| mode | local | multihop | paraphrase | **ALL** | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| `lexical_v0` (baseline) | 0.615 | 0.667 | **0.000** | 0.370 | 73 | 90 |
| `bm25` | 0.692 | 0.667 | 0.000 | 0.407 | **16** | 24 |
| `vector` | 0.692 | 0.667 | **0.182** | 0.481 | 49¹ | 53¹ |
| **`hybrid` (default)** | **0.769** | **1.000** | **0.182** | **0.556** | **71**¹ | 79¹ |

¹ warm, with the query-embedding cache populated. See *Latency* below.

**ALL recall +50% relative** (0.370 → 0.556) at the same latency as the old
lexical path. Multihop is now perfect on this set.

## What each phase actually contributed

Phase 1 measured flat on this harness (see `PHASE1-GATE.md`) because the harness
measures page search and Phase 1 changed read time and fact ranking. Phase 2 is
the first change to `t_search` itself, which is why it moves.

Two separate wins, worth not conflating:

- **BM25 alone beats the old lexical scorer** (0.407 vs 0.370) while being
  **4.5× faster** (16 ms vs 73 ms). `str.count()` over raw page text was never
  a relevance model. Some of the "vector" win is really "we finally had a
  lexical baseline worth fusing with" — the plan warned about exactly this.
- **Embeddings unlock the paraphrase class** from a literal zero. Nothing
  lexical can do this: `all(t in low for t in terms)` cannot match a token that
  is not present.

## The honest weakness

**Paraphrase recall is 0.182, not 0.8.** Two of eleven. That is a real
improvement over zero, but it is not a solved problem, and it should not be
reported as one.

Inspecting the nine misses, the failure is *precision, not blindness*: the
vector path returns semantically adjacent but wrong pages.

| query | wanted | got |
|---|---|---|
| "what cheap adjustment lets related notes participate in lookup" | vault retrieval audit | `Brian Sheridan` |
| "why is the wiki behaving like find-in-page" | vault retrieval audit | `Topic - personal-memory` |
| "how do I keep an analysis from vanishing when the chat ends" | vault cheat sheet | `long-term-memory` overview |

Those are not absurd neighbours — they are the right *neighbourhood*, wrong
page. That points at chunk granularity and the absence of a reranker, not at the
embedding model.

**Candidate next steps, in order of expected value per unit of effort:**

1. **A reranker over the fused top-20.** Anthropic measured a reranker as the
   single largest contributor to their reported reduction in top-20 retrieval
   failures. This is the natural next move and does not require re-embedding.
2. **T2.3 contextual retrieval** — prepend a "where this chunk lives" header
   before indexing. The plan gates this behind measurement, and this is the
   measurement: it is now justified to try.
3. **Tune chunk size.** 1200 chars was chosen, not derived. The harness can
   settle it.

## Latency — read this before quoting a number

Cold (first time a query string is ever seen) the vector path costs **~700 ms**,
essentially all of it the network round-trip to Azure OpenAI to embed the query.
That **fails** the plan's <300 ms bar.

A query-embedding cache in the sidecar fixes the repeat case: warm hybrid p50 is
**71 ms**, against the lexical baseline's 73 ms. Agents re-issue queries
constantly, so warm is the common case — but a genuinely novel query still pays
the round-trip, and on a slow or absent network it falls back to lexical.

This is an inherent cost of choosing a hosted embedding API over a local model.
It was a deliberate choice; it is not free, and the cold number should be quoted
whenever latency is discussed.

## Cost and footprint

| | |
|---|---|
| Corpus | 619 pages → 7,940 chunks |
| One-off embedding cost | **$0.039** at $0.02/1M tokens |
| Re-index cost | **$0.00** — content-hash keying reused 7,940/7,940 |
| Sidecar size | 20.9 MB (vectors) → 34.9 MB (with BM25 index) |
| Dimensions | 512 (Matryoshka truncation of `text-embedding-3-small`) |

512 rather than the full 1536: MTEB 61.6 vs 62.3, for a 3× smaller sidecar.
At 1536 the file would be ~78 MB, which is not a sensible thing to commit.

## Safety properties held

- `mode='lexical'` is byte-identical to the pre-Phase-2 scorer and is the
  guaranteed-offline fallback.
- Every semantic path degrades to lexical on any failure: sqlite-vec missing,
  endpoint unset, `az login` lapsed, network down, nothing indexed.
- A dimension mismatch between config and a stored sidecar is **refused**, not
  silently served — comparing truncated vectors would return confident nonsense.
- 239 tests green; greenfield bootstrap green; MCP conformance 9/9.

## Reproducing

```powershell
python wiki\vault_vectors.py status
python wiki\vault_vectors.py index          # $0 when nothing changed
python wiki\vault_vectors.py search "query"
python ops\retrieval_bench.py --compare docs\bench\baseline-2026-09-02.json docs\bench\phase1-2026-09-02.json
```
