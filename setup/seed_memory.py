#!/usr/bin/env python3
"""Seed a neutral, representative memory database for smoke testing.

Run once: ``python seed_memory.py``. The seed is idempotent and deliberately
contains no personal, customer, or employee-relations data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wiki"))
from config import paths

DB = paths()["memory_db"]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def upsert_topic(conn, slug, title, category, description):
    conn.execute(
        """INSERT INTO topics(slug, title, category, description, status)
           VALUES(?,?,?,?, 'active')
           ON CONFLICT(slug) DO UPDATE SET
             title=excluded.title,
             description=excluded.description""",
        (slug, title, category, description),
    )
    return conn.execute("SELECT id FROM topics WHERE slug=?", (slug,)).fetchone()[0]


def upsert_entity(conn, name, entity_type, notes, attributes="{}"):
    conn.execute(
        """INSERT INTO entities(name, entity_type, notes, attributes)
           VALUES(?,?,?,?)
           ON CONFLICT(name, entity_type) DO UPDATE SET
             notes=excluded.notes, attributes=excluded.attributes""",
        (name, entity_type, notes, attributes),
    )
    return conn.execute(
        "SELECT id FROM entities WHERE name=? AND entity_type=?", (name, entity_type)
    ).fetchone()[0]


def add_fact(conn, topic_id, content, fact_type, confidence, importance, source):
    row = conn.execute(
        "SELECT id FROM facts WHERE topic_id IS ? AND content=?", (topic_id, content)
    ).fetchone()
    if row:
        return row[0], False
    cur = conn.execute(
        """INSERT INTO facts
           (topic_id, content, fact_type, confidence, importance, source)
           VALUES(?,?,?,?,?,?)""",
        (topic_id, content, fact_type, confidence, importance, source),
    )
    return cur.lastrowid, True


def add_snapshot(conn, topic_id, title, summary, findings, decisions, next_steps):
    row = conn.execute(
        "SELECT id FROM snapshots WHERE topic_id=? AND title=?", (topic_id, title)
    ).fetchone()
    if row:
        return row[0], False
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq_number), 0) + 1 FROM snapshots WHERE topic_id=?",
        (topic_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO snapshots
           (topic_id, seq_number, title, summary, findings, decisions, next_steps)
           VALUES(?,?,?,?,?,?,?)""",
        (topic_id, seq, title, summary, findings, decisions, next_steps),
    )
    return cur.lastrowid, True


def add_mention(conn, entity_id, parent_id, parent_type, relationship="mentions"):
    conn.execute(
        """INSERT OR IGNORE INTO entity_mentions
           (entity_id, parent_id, parent_type, relationship) VALUES(?,?,?,?)""",
        (entity_id, parent_id, parent_type, relationship),
    )


def add_ref(conn, parent_id, parent_type, ref_type, ref_value, label):
    conn.execute(
        """INSERT OR IGNORE INTO refs
           (parent_id, parent_type, ref_type, ref_value, label) VALUES(?,?,?,?,?)""",
        (parent_id, parent_type, ref_type, ref_value, label),
    )


def seed(conn):
    topic_id = upsert_topic(
        conn,
        "sample-service-review",
        "Sample Service Review",
        "technical",
        "Neutral sample data for validating long-term-memory retrieval and exports.",
    )
    account_id = upsert_entity(
        conn,
        "Example Operations",
        "org",
        "Fictional organization used only by the neutral memory seed.",
        json.dumps({"environment": "sample", "data_classification": "synthetic"}),
    )
    product_id = upsert_entity(
        conn,
        "Example Analyzer",
        "product",
        "Fictional analysis tool used only by the neutral memory seed.",
    )

    facts = [
        ("Example Operations reduced sample processing time by 40% after enabling Example Analyzer.",
         "finding", 5, 4, "Synthetic service review"),
        ("Do not publish a sample result until the validation checklist is complete.",
         "constraint", 5, 5, "Synthetic service review"),
        ("Use Example Analyzer for the next sample batch and compare results with the baseline.",
         "decision", 4, 3, "Synthetic service review"),
        ("Prefer documented, repeatable measurements over anecdotal sample feedback.",
         "preference", 4, 3, "Synthetic service review"),
        ("Measure whether the sample processing improvement holds at a larger volume.",
         "question", 3, 2, "Synthetic service review"),
    ]
    fact_ids = []
    inserted = 0
    for content, fact_type, confidence, importance, source in facts:
        fact_id, is_new = add_fact(
            conn, topic_id, content, fact_type, confidence, importance, source
        )
        fact_ids.append(fact_id)
        inserted += is_new

    snapshot_id, snapshot_new = add_snapshot(
        conn,
        topic_id,
        "Sample Service Review - Initial Findings",
        "A neutral sample validates the topic, fact, entity, snapshot, and reference surfaces.",
        "Processing time improved in the synthetic scenario.",
        "Use the analyzer for the next sample batch.",
        "Repeat the measurement at a larger volume.",
    )

    add_mention(conn, account_id, topic_id, "topic", "about")
    add_mention(conn, product_id, topic_id, "topic", "references")
    add_mention(conn, account_id, fact_ids[0], "fact")
    add_mention(conn, product_id, fact_ids[0], "fact")
    add_mention(conn, product_id, snapshot_id, "snapshot", "references")
    add_ref(
        conn,
        topic_id,
        "topic",
        "document",
        "sample-service-review.md",
        "Synthetic service review source",
    )
    add_ref(
        conn,
        fact_ids[0],
        "fact",
        "url",
        "https://example.invalid/sample-service-review",
        "Synthetic reference URL",
    )
    return inserted, int(snapshot_new)


def main():
    print(f"Seeding neutral sample data: {DB}")
    with get_conn() as conn:
        fact_count, snapshot_count = seed(conn)
    with get_conn() as conn:
        stats = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("entities", "topics", "facts", "entity_mentions", "snapshots", "refs")
        }
    print(f"Inserted {fact_count} fact(s) and {snapshot_count} snapshot(s).")
    print(f"Final counts: {stats}")


if __name__ == "__main__":
    main()
