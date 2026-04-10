#!/usr/bin/env python3
"""Tests for ltm_session_end.py — uses in-memory and temp DBs to avoid touching real data."""

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ltm_session_end import (
    process_session,
    get_conn,
    _ensure_tables,
    extract_entities,
    extract_facts,
    extract_pending_work,
    classify_session,
    touch_relevant_topics,
)

INIT_SQL = (Path(__file__).parent / "init-memory.sql").read_text(encoding="utf-8")


def _make_store_db(path: str, sessions: list[dict], turns: list[dict]) -> None:
    """Create a minimal session-store.db with given data."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT,
            branch TEXT, summary TEXT, created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE turns (
            session_id TEXT, turn_index INTEGER,
            user_message TEXT, assistant_response TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE session_files (
            session_id TEXT, file_path TEXT, tool_name TEXT
        )
    """)
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
            (s["id"], s["cwd"], s.get("repository"), s.get("branch"),
             s.get("summary"), s.get("created_at", "2025-01-01T00:00:00")),
        )
    for t in turns:
        conn.execute(
            "INSERT INTO turns VALUES(?,?,?,?)",
            (t["session_id"], t["turn_index"],
             t.get("user_message"), t.get("assistant_response")),
        )
    conn.commit()
    conn.close()


def _make_memory_db(path: str) -> None:
    """Create a memory.db with the full LTM schema."""
    conn = sqlite3.connect(path)
    conn.executescript(INIT_SQL)
    conn.commit()
    conn.close()


class TestSessionEnd(unittest.TestCase):
    """Integration tests for the LTM session-end hook."""

    def setUp(self):
        import uuid
        self.test_dir = os.path.join(os.path.dirname(__file__), f"_test_{uuid.uuid4().hex[:8]}")
        os.makedirs(self.test_dir, exist_ok=True)
        self.store_path = os.path.join(self.test_dir, "session-store.db")
        self.memory_path = os.path.join(self.test_dir, "memory.db")

    def tearDown(self):
        import shutil, gc, time
        gc.collect()
        time.sleep(0.1)
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def _setup_dbs(self, sessions, turns, seed_entities=None, seed_topics=None):
        _make_store_db(self.store_path, sessions, turns)
        _make_memory_db(self.memory_path)
        if seed_entities or seed_topics:
            conn = get_conn(self.memory_path)
            _ensure_tables(conn)
            for e in (seed_entities or []):
                conn.execute(
                    "INSERT OR IGNORE INTO entities(name, entity_type) VALUES(?,?)",
                    (e["name"], e["entity_type"]),
                )
            for t in (seed_topics or []):
                conn.execute(
                    "INSERT OR IGNORE INTO topics(slug, title, category) VALUES(?,?,?)",
                    (t["slug"], t["title"], t.get("category", "research")),
                )
            conn.commit()
            conn.close()

    # ── Test: entity extraction ───────────────────────────────────────

    def test_extract_people_entities(self):
        sessions = [{"id": "s1", "cwd": "/workspace/agency",
                     "summary": "Customer meeting prep"}]
        turns = [
            {"session_id": "s1", "turn_index": 0,
             "user_message": "I had a meeting with Sarah Johnson about the migration",
             "assistant_response": "Got it. Sarah Johnson's migration plan looks solid."},
            {"session_id": "s1", "turn_index": 1,
             "user_message": "John Smith mentioned the timeline is Q3",
             "assistant_response": "Noted. Q3 timeline from John Smith."},
        ]
        self._setup_dbs(sessions, turns)
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        self.assertIsNotNone(result)

        conn = get_conn(self.memory_path)
        people = conn.execute(
            "SELECT name FROM entities WHERE entity_type='person'"
        ).fetchall()
        names = {r["name"] for r in people}
        self.assertIn("Sarah Johnson", names)
        self.assertIn("John Smith", names)
        conn.close()

    # ── Test: fact extraction (decisions) ─────────────────────────────

    def test_extract_decision_facts(self):
        sessions = [{"id": "s2", "cwd": "/workspace/agency",
                     "summary": "Architecture discussion"}]
        turns = [
            {"session_id": "s2", "turn_index": 0,
             "user_message": "let's go with Azure Functions for the webhook handler",
             "assistant_response": "Sounds good, Azure Functions it is."},
            {"session_id": "s2", "turn_index": 1,
             "user_message": "actually use Cosmos DB instead of Table Storage",
             "assistant_response": "Switching to Cosmos DB."},
        ]
        self._setup_dbs(sessions, turns)
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["facts"], 2)

        conn = get_conn(self.memory_path)
        facts = conn.execute(
            "SELECT content, fact_type FROM facts WHERE source='auto-extracted from session'"
        ).fetchall()
        types = {r["fact_type"] for r in facts}
        self.assertIn("decision", types)
        self.assertIn("preference", types)
        conn.close()

    # ── Test: pending work detection ──────────────────────────────────

    def test_pending_work_on_abort(self):
        sessions = [{"id": "s3", "cwd": "/workspace/agency",
                     "summary": "Debugging throttle"}]
        turns = [
            {"session_id": "s3", "turn_index": 0,
             "user_message": "fix the 429 errors",
             "assistant_response": "I've fixed the retry logic. Still need to update the tests for the rate limiter."},
        ]
        self._setup_dbs(sessions, turns)
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "user_exit"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        self.assertIsNotNone(result)

        conn = get_conn(self.memory_path)
        todos = conn.execute(
            "SELECT content FROM facts WHERE fact_type='todo'"
        ).fetchall()
        self.assertGreaterEqual(len(todos), 1)
        self.assertTrue(any("still need" in r["content"].lower() for r in todos))
        conn.close()

    # ── Test: no pending work on clean exit ───────────────────────────

    def test_no_pending_work_on_complete(self):
        sessions = [{"id": "s4", "cwd": "/workspace/agency", "summary": "Done"}]
        turns = [
            {"session_id": "s4", "turn_index": 0,
             "user_message": "hello",
             "assistant_response": "Still need to do something."},
        ]
        self._setup_dbs(sessions, turns)
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        conn = get_conn(self.memory_path)
        todos = conn.execute("SELECT * FROM facts WHERE fact_type='todo'").fetchall()
        self.assertEqual(len(todos), 0)
        conn.close()

    # ── Test: session pattern classification ──────────────────────────

    def test_pattern_classification(self):
        sessions = [{"id": "s5", "cwd": "/workspace/agency",
                     "summary": "MSX pipeline review and milestone updates"}]
        turns = [
            {"session_id": "s5", "turn_index": 0,
             "user_message": "show my MSX pipeline and opportunity details",
             "assistant_response": "Here are your milestone and pipeline updates."},
            {"session_id": "s5", "turn_index": 1,
             "user_message": "update the opportunity forecast",
             "assistant_response": "Updated the MSX opportunity."},
        ]
        self._setup_dbs(sessions, turns)
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        self.assertIn("msx-activity", result["patterns"])

        conn = get_conn(self.memory_path)
        patterns = conn.execute(
            "SELECT pattern_type FROM ltm_patterns WHERE session_id='s5'"
        ).fetchall()
        self.assertTrue(any(r["pattern_type"] == "msx-activity" for r in patterns))
        conn.close()

    # ── Test: idempotency ─────────────────────────────────────────────

    def test_idempotency(self):
        sessions = [{"id": "s6", "cwd": "/workspace/agency", "summary": "Test"}]
        turns = [
            {"session_id": "s6", "turn_index": 0,
             "user_message": "let's go with Redis for caching",
             "assistant_response": "Using Redis."},
        ]
        self._setup_dbs(sessions, turns)
        hook = {"cwd": "/workspace/agency", "reason": "complete"}
        r1 = process_session(hook, memory_db=self.memory_path,
                             store_path=Path(self.store_path))
        r2 = process_session(hook, memory_db=self.memory_path,
                             store_path=Path(self.store_path))
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)  # second run returns None (already processed)

    # ── Test: topic touching ──────────────────────────────────────────

    def test_touch_topics(self):
        sessions = [{"id": "s7", "cwd": "/workspace/agency",
                     "summary": "GCC discussion"}]
        turns = [
            {"session_id": "s7", "turn_index": 0,
             "user_message": "What are the gcc-gaps for M365 Copilot?",
             "assistant_response": "Here are the GCC product gaps."},
        ]
        self._setup_dbs(sessions, turns,
                        seed_topics=[{"slug": "gcc-gaps", "title": "GCC Product Gaps"}])
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        conn = get_conn(self.memory_path)
        row = conn.execute(
            "SELECT last_accessed_at FROM topics WHERE slug='gcc-gaps'"
        ).fetchone()
        self.assertIsNotNone(row["last_accessed_at"])
        conn.close()

    # ── Test: account entity matching ─────────────────────────────────

    def test_account_entity_mention(self):
        sessions = [{"id": "s8", "cwd": "/workspace/agency",
                     "summary": "Contoso migration"}]
        turns = [
            {"session_id": "s8", "turn_index": 0,
             "user_message": "Review the Contoso Azure migration plan",
             "assistant_response": "Contoso's migration is on track."},
        ]
        self._setup_dbs(
            sessions, turns,
            seed_entities=[{"name": "Contoso", "entity_type": "account"}],
            seed_topics=[{"slug": "contoso-migration", "title": "Contoso Migration"}],
        )
        result = process_session(
            {"cwd": "/workspace/agency", "reason": "complete"},
            memory_db=self.memory_path,
            store_path=Path(self.store_path),
        )
        conn = get_conn(self.memory_path)
        mentions = conn.execute(
            "SELECT * FROM entity_mentions WHERE parent_type='topic'"
        ).fetchall()
        self.assertGreaterEqual(len(mentions), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
