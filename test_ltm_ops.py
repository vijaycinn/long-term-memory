#!/usr/bin/env python3
"""
test_ltm_ops.py — Tests for retention (ltm_maintain) and continuity (ltm_backup).

The continuity tests deliberately destroy a database and restore it, because a
backup that has never been restored is a hypothesis, not a guarantee.
"""

import datetime
import os
import shutil
import sqlite3
import tempfile
import unittest

import ltm_backup
import ltm_maintain

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "init-memory.sql")


def _days_ago(n):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class OpsTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ltm_ops_test_")
        self.db = os.path.join(self.dir, "memory.db")
        self.backups = os.path.join(self.dir, "backups")
        conn = sqlite3.connect(self.db)
        conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def exec(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def query(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def add_topic(self, slug, accessed_days_ago=0, status="active"):
        return self.exec(
            "INSERT INTO topics(slug,title,category,status,last_accessed_at) VALUES(?,?,?,?,?)",
            (slug, slug, "research", status, _days_ago(accessed_days_ago)),
        )

    def add_fact(self, content, topic_id=None, status="active", accessed_days_ago=0):
        return self.exec(
            "INSERT INTO facts(topic_id,content,fact_type,importance,status,last_accessed_at) "
            "VALUES(?,?,?,?,?,?)",
            (topic_id, content, "finding", 4, status, _days_ago(accessed_days_ago)),
        )


# ── retention ─────────────────────────────────────────────────────────

class TestArchiveStaleTopics(OpsTestCase):
    def setUp(self):
        super().setUp()
        self.stale = self.add_topic("stale", accessed_days_ago=400)
        self.fresh = self.add_topic("fresh", accessed_days_ago=2)
        self.protected = self.add_topic("protected", accessed_days_ago=400)
        self.add_fact("recent activity here", self.protected, accessed_days_ago=1)

    def test_dry_run_changes_nothing(self):
        res = ltm_maintain.run_maintenance({"db_path": self.db})
        self.assertTrue(res["dry_run"])
        self.assertGreaterEqual(res["archive_stale_topics"]["count"], 1)
        active = self.query("SELECT count(*) FROM topics WHERE status='active'")[0][0]
        self.assertEqual(active, 3)

    def test_apply_archives_only_stale(self):
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        rows = dict(self.query("SELECT slug, status FROM topics"))
        self.assertEqual(rows["stale"], "archived")
        self.assertEqual(rows["fresh"], "active")

    def test_topic_with_recent_facts_is_protected(self):
        # An idle topic that still has fresh facts is real work in progress.
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        rows = dict(self.query("SELECT slug, status FROM topics"))
        self.assertEqual(rows["protected"], "active")

    def test_threshold_is_configurable(self):
        res = ltm_maintain.run_maintenance({"db_path": self.db, "stale_topic_days": 1000})
        self.assertEqual(res["archive_stale_topics"]["count"], 0)

    def test_archived_topic_keeps_its_facts(self):
        self.add_fact("old fact", self.stale, accessed_days_ago=400)
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        n = self.query("SELECT count(*) FROM facts WHERE topic_id=?", (self.stale,))[0][0]
        self.assertEqual(n, 1)


class TestPruneSuperseded(OpsTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.add_topic("t")
        self.add_fact("old superseded", self.t, status="superseded", accessed_days_ago=200)
        self.add_fact("recent superseded", self.t, status="superseded", accessed_days_ago=5)
        self.add_fact("old but active", self.t, status="active", accessed_days_ago=400)

    def test_prunes_only_cold_retired_facts(self):
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        remaining = [r[0] for r in self.query("SELECT content FROM facts")]
        self.assertNotIn("old superseded", remaining)
        self.assertIn("recent superseded", remaining)

    def test_never_prunes_active_facts(self):
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        remaining = [r[0] for r in self.query("SELECT content FROM facts")]
        self.assertIn("old but active", remaining)

    def test_dry_run_reports_without_deleting(self):
        res = ltm_maintain.run_maintenance({"db_path": self.db})
        self.assertEqual(res["prune_superseded"]["count"], 1)
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 3)

    def test_pruned_rows_leave_no_fts_orphans(self):
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        fts = self.query("SELECT count(*) FROM memory_fts WHERE table_name='facts'")[0][0]
        facts = self.query("SELECT count(*) FROM facts")[0][0]
        self.assertEqual(fts, facts)


class TestDedupe(OpsTestCase):
    def test_removes_duplicate_global_facts(self):
        # topic_id NULL escapes UNIQUE(topic_id, content) because NULL != NULL.
        self.add_fact("identical global fact", None)
        self.add_fact("identical global fact", None)
        self.add_fact("identical global fact", None)
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 3)

        res = ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        self.assertEqual(res["dedupe_facts"]["count"], 2)
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 1)

    def test_keeps_distinct_facts(self):
        self.add_fact("alpha", None)
        self.add_fact("beta", None)
        ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 2)


class TestCompact(OpsTestCase):
    def test_reclaims_space_after_deletion(self):
        t = self.add_topic("bulk")
        for i in range(400):
            self.add_fact(f"bulk fact {i} " + ("x" * 200), t,
                          status="superseded", accessed_days_ago=300)
        res = ltm_maintain.run_maintenance({"db_path": self.db, "apply": True})
        self.assertGreater(res["compact"]["reclaimed_bytes"], 0)

    def test_dry_run_does_not_compact(self):
        res = ltm_maintain.run_maintenance({"db_path": self.db})
        self.assertFalse(res["compact"]["applied"])


# ── continuity ────────────────────────────────────────────────────────

class TestBackup(OpsTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.add_topic("bc")
        for i in range(25):
            self.add_fact(f"continuity fact {i}", self.t)

    def test_backup_is_created_and_verified(self):
        res = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.exists(res["path"]))
        self.assertEqual(res["counts"]["facts"], 25)

    def test_backup_passes_integrity_check(self):
        res = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        self.assertTrue(ltm_backup.verify({"path": res["path"]})["ok"])

    def test_backup_is_consistent_under_open_wal(self):
        # Hold an open connection with uncommitted-then-committed work, proving
        # the online backup API beats a naive file copy.
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO facts(topic_id,content,fact_type,importance,status) "
            "VALUES(?,?,?,?,?)", (self.t, "written in wal", "finding", 4, "active"))
        conn.commit()
        try:
            res = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
            self.assertTrue(res["ok"])
            self.assertEqual(res["counts"]["facts"], 26)
        finally:
            conn.close()

    def test_rotation_keeps_only_n(self):
        for _ in range(5):
            ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups, "keep": 3})
        self.assertLessEqual(
            ltm_backup.list_backups({"backup_dir": self.backups})["count"], 3)

    def test_mirror_copy_written(self):
        mirror = os.path.join(self.dir, "mirror")
        res = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups,
                                 "mirror_dir": mirror})
        self.assertTrue(res["mirror"]["ok"])
        self.assertTrue(os.path.exists(res["mirror"]["path"]))

    def test_corrupt_source_is_reported(self):
        bad = os.path.join(self.dir, "not-a-db.db")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a sqlite database")
        self.assertFalse(ltm_backup.verify({"path": bad})["ok"])

    def test_same_second_backups_do_not_overwrite(self):
        # Second-granular stamps collide; each snapshot is still a distinct file.
        a = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        b = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        self.assertNotEqual(a["path"], b["path"])
        self.assertTrue(os.path.exists(a["path"]))
        self.assertTrue(os.path.exists(b["path"]))
        self.assertEqual(
            ltm_backup.list_backups({"backup_dir": self.backups})["count"], 2)


class TestRestoreDrill(OpsTestCase):
    """The actual disaster drill: destroy the live DB, restore, prove equality."""

    def setUp(self):
        super().setUp()
        self.t = self.add_topic("drill")
        for i in range(40):
            self.add_fact(f"irreplaceable fact {i}", self.t)
        self.snapshot = ltm_backup.backup(
            {"db_path": self.db, "backup_dir": self.backups})
        self.assertTrue(self.snapshot["ok"])

    def test_restore_requires_confirmation(self):
        res = ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups})
        self.assertTrue(res.get("requires_confirm"))

    def test_total_loss_is_recoverable(self):
        os.remove(self.db)
        self.assertFalse(os.path.exists(self.db))

        res = ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups,
                                  "confirm": True})
        self.assertTrue(res["ok"])
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 40)

    def test_corruption_is_recoverable(self):
        with open(self.db, "wb") as fh:
            fh.write(b"\x00" * 4096)          # shred the header
        self.assertFalse(ltm_backup.verify({"path": self.db})["ok"])

        res = ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups,
                                  "confirm": True})
        self.assertTrue(res["ok"])
        self.assertEqual(self.query("SELECT count(*) FROM facts")[0][0], 40)

    def test_restore_keeps_pre_restore_copy(self):
        res = ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups,
                                  "confirm": True})
        self.assertTrue(os.path.exists(res["pre_restore_copy"]))

    def test_restored_content_matches_exactly(self):
        original = sorted(r[0] for r in self.query("SELECT content FROM facts"))
        os.remove(self.db)
        ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups,
                            "confirm": True})
        restored = sorted(r[0] for r in self.query("SELECT content FROM facts"))
        self.assertEqual(original, restored)

    def test_search_works_after_restore(self):
        os.remove(self.db)
        ltm_backup.restore({"db_path": self.db, "backup_dir": self.backups,
                            "confirm": True})
        hits = self.query(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'irreplaceable'")
        self.assertGreater(hits[0][0], 0)

    def test_refuses_to_restore_a_corrupt_backup(self):
        bad = os.path.join(self.backups, "memory-99999999-999999.db")
        with open(bad, "wb") as fh:
            fh.write(b"garbage")
        res = ltm_backup.restore({"db_path": self.db, "backup": bad, "confirm": True})
        self.assertFalse(res["ok"])
        self.assertIn("verification", res["error"])


class TestStatus(OpsTestCase):
    def test_at_risk_when_no_backups(self):
        res = ltm_backup.status({"db_path": self.db, "backup_dir": self.backups})
        self.assertEqual(res["posture"], "AT RISK")

    def test_healthy_after_backup(self):
        ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        res = ltm_backup.status({"db_path": self.db, "backup_dir": self.backups})
        self.assertEqual(res["posture"], "HEALTHY")
        self.assertIsNotNone(res["rpo_hours"])

    def test_degraded_when_backup_is_stale(self):
        res = ltm_backup.backup({"db_path": self.db, "backup_dir": self.backups})
        old = datetime.datetime.now() - datetime.timedelta(hours=100)
        os.utime(res["path"], (old.timestamp(), old.timestamp()))
        status = ltm_backup.status({"db_path": self.db, "backup_dir": self.backups})
        self.assertEqual(status["posture"], "DEGRADED")


class TestAutoBackup(OpsTestCase):
    """Continuity must be automatic, not dependent on remembering to run it."""

    def setUp(self):
        super().setUp()
        import ltm_session_end
        self.hook = ltm_session_end
        self.t = self.add_topic("auto")
        self.add_fact("auto backup fact", self.t)
        self.payload = {"db_path": self.db, "backup_dir": self.backups}

    def test_creates_backup_when_none_exists(self):
        res = self.hook.auto_backup(self.payload)
        self.assertTrue(res["ok"])
        self.assertEqual(
            ltm_backup.list_backups({"backup_dir": self.backups})["count"], 1)

    def test_throttled_on_back_to_back_sessions(self):
        self.hook.auto_backup(self.payload)
        second = self.hook.auto_backup(self.payload)
        self.assertTrue(second.get("skipped"))
        self.assertEqual(
            ltm_backup.list_backups({"backup_dir": self.backups})["count"], 1)

    def test_backs_up_again_once_interval_elapses(self):
        first = self.hook.auto_backup(self.payload)
        old = datetime.datetime.now() - datetime.timedelta(hours=48)
        os.utime(first["path"], (old.timestamp(), old.timestamp()))
        second = self.hook.auto_backup(self.payload)
        self.assertTrue(second["ok"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(
            ltm_backup.list_backups({"backup_dir": self.backups})["count"], 2)

    def test_never_raises_on_bad_input(self):
        # A hook that throws would break session shutdown, so this must be silent.
        try:
            self.hook.auto_backup({"db_path": "/nonexistent/path/x.db",
                                   "backup_dir": self.backups})
        except Exception as exc:
            self.fail(f"auto_backup must never raise, got {type(exc).__name__}: {exc}")

    def test_survives_missing_database(self):
        res = self.hook.auto_backup({"db_path": os.path.join(self.dir, "missing.db"),
                                     "backup_dir": self.backups})
        self.assertFalse((res or {}).get("ok", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
