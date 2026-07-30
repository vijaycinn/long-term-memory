#!/usr/bin/env python3
"""
ltm_bench.py — Scale benchmark for the long-term memory store.

Answers one question with numbers instead of intuition: does LTM stay
performant as facts, topics and snapshots accumulate day after day?

Builds throwaway databases at several sizes, then times the operations that
actually run in a session:

    export_context   every session start / Remember-This  (latency critical)
    search_memory    every lookup                          (latency critical)
    add_fact         every capture                         (throughput)
    migrate          occasional maintenance

Usage:
    python ltm_bench.py                      # default ladder
    python ltm_bench.py --sizes 1000,10000   # custom
    python ltm_bench.py --json               # machine readable
"""

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

import memory_driver as md

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "init-memory.sql")

WORDS = (
    "foundry anthropic databases frontier azure copilot github territory quota "
    "attainment milestone partner consumption workload migration governance "
    "compliance tenant subscription pipeline forecast escalation architecture"
).split()


def _sentence(rng, n=24):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def build_db(path, n_facts, n_topics, n_entities=200, n_snapshots=None):
    """Create a synthetic database of the requested shape."""
    if n_snapshots is None:
        n_snapshots = n_topics
    rng = random.Random(1234)

    conn = sqlite3.connect(path)
    conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
    conn.execute("PRAGMA synchronous = OFF")

    now = md._now()
    topic_ids = []
    for i in range(n_topics):
        cur = conn.execute(
            "INSERT INTO topics(slug, title, category, description, status, last_accessed_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"topic-{i}", f"Topic {i}", "research", _sentence(rng, 8), "active", now),
        )
        topic_ids.append(cur.lastrowid)

    types = ["insight", "decision", "finding", "action", "question",
             "constraint", "todo", "preference"]
    rows = []
    for i in range(n_facts):
        rows.append((
            rng.choice(topic_ids),
            f"fact {i} :: {_sentence(rng)}",
            rng.choice(types),
            rng.randint(1, 5),
            rng.randint(1, 5),
            "active",
            "bench",
            now,
        ))
    conn.executemany(
        "INSERT INTO facts(topic_id, content, fact_type, confidence, importance, "
        "status, source, last_accessed_at) VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )

    conn.executemany(
        "INSERT INTO entities(name, entity_type, is_self, notes) VALUES(?,?,0,?)",
        [(f"Entity {i}", "account", _sentence(rng, 6)) for i in range(n_entities)],
    )
    conn.execute(
        "INSERT INTO entities(name, entity_type, is_self, notes) VALUES('Bench User','person',1,'self')"
    )

    conn.executemany(
        "INSERT INTO snapshots(topic_id, seq_number, title, summary, findings, next_steps) "
        "VALUES(?,?,?,?,?,?)",
        [(topic_ids[i % len(topic_ids)], 1, f"Snapshot {i}", _sentence(rng, 10),
          _sentence(rng, 20), _sentence(rng, 10)) for i in range(n_snapshots)],
    )

    conn.commit()
    conn.close()


def time_it(fn, repeat=5):
    """Return (median_ms, p95_ms) over `repeat` runs."""
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return round(statistics.median(samples), 1), round(p95, 1)


def bench_one(n_facts, n_topics, repeat=5):
    tmp = tempfile.mkdtemp(prefix="ltm_bench_")
    db = os.path.join(tmp, "memory.db")
    ctx = os.path.join(tmp, "memory-context.md")
    try:
        t0 = time.perf_counter()
        build_db(db, n_facts, n_topics)
        build_s = round(time.perf_counter() - t0, 1)

        base = {"db_path": db, "context_path": ctx}

        export_ms = time_it(lambda: md.export_context(dict(base)), repeat)
        search_ms = time_it(lambda: md.search_memory(dict(base, query="foundry")), repeat)
        search_hyphen_ms = time_it(
            lambda: md.search_memory(dict(base, query="topic-7")), repeat)
        addfact_ms = time_it(
            lambda: md.add_fact(dict(base, content=f"probe {time.perf_counter()}",
                                     importance=4)), repeat)
        stats_ms = time_it(lambda: md.get_stats(dict(base)), repeat)

        size_mb = round(os.path.getsize(db) / (1024 * 1024), 2)
        fts_rows = sqlite3.connect(db).execute(
            "SELECT count(*) FROM memory_fts").fetchone()[0]

        return {
            "facts": n_facts,
            "topics": n_topics,
            "db_mb": size_mb,
            "fts_rows": fts_rows,
            "build_s": build_s,
            "export_ms": export_ms[0],
            "export_p95_ms": export_ms[1],
            "search_ms": search_ms[0],
            "search_hyphen_ms": search_hyphen_ms[0],
            "add_fact_ms": addfact_ms[0],
            "stats_ms": stats_ms[0],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


LADDER = [(1000, 25), (10000, 100), (50000, 500)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", help="comma list of facts counts, e.g. 1000,10000")
    ap.add_argument("--topics", type=int, help="fixed topic count override")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ladder = LADDER
    if args.sizes:
        counts = [int(x) for x in args.sizes.split(",")]
        ladder = [(c, args.topics or max(25, c // 100)) for c in counts]

    results = [bench_one(f, t, args.repeat) for f, t in ladder]

    if args.json:
        print(json.dumps(results, indent=2))
        return

    hdr = (f"{'facts':>7} {'topics':>7} {'db MB':>7} {'ftsRows':>8} "
           f"{'export':>9} {'exp p95':>9} {'search':>8} {'srch-hyp':>9} "
           f"{'add':>7} {'stats':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['facts']:>7} {r['topics']:>7} {r['db_mb']:>7} {r['fts_rows']:>8} "
              f"{r['export_ms']:>8}ms {r['export_p95_ms']:>8}ms {r['search_ms']:>7}ms "
              f"{r['search_hyphen_ms']:>8}ms {r['add_fact_ms']:>6}ms {r['stats_ms']:>6}ms")

    # Scaling verdict: compare largest vs smallest.
    if len(results) >= 2:
        a, b = results[0], results[-1]
        growth = b["facts"] / max(a["facts"], 1)
        print()
        for key, label in (("export_ms", "export_context"), ("search_ms", "search")):
            factor = b[key] / max(a[key], 0.01)
            verdict = "sub-linear" if factor < growth * 0.5 else (
                "linear" if factor < growth * 1.5 else "SUPER-LINEAR")
            print(f"  {label:<16} {a[key]}ms -> {b[key]}ms "
                  f"({factor:.1f}x for {growth:.0f}x data) = {verdict}")


if __name__ == "__main__":
    main()
