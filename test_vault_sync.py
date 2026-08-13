"""Tests for the wiki bridge wired into LTM persistence.

Three contracts are guarded here:

1. `sync_obsidian_vault` in the sessionEnd hook — it must never raise, must
   honour the opt-out, and must be a no-op when the wiki layer is absent.
2. `wiki/sync.py` — the marker, the lock and the retry-on-failure behaviour.
   This moved out of the hook so PowerShell, Python and the hook share one
   implementation instead of three.
3. `clear_generated(origin=...)` in the builder — an incremental rebuild must
   delete only the notes it owns. This regressed once already, silently
   dropping imported documents that lived in a folder treated as memory-derived.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIKI = HERE / "wiki"
sys.path.insert(0, str(HERE))

import ltm_session_end as se


def _fake_wiki(d: Path, exit_code: int = 0) -> Path:
    """A stand-in sync.py so hook behaviour is tested without a real build."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "sync.py").write_text(f"raise SystemExit({exit_code})", encoding="utf-8")
    return d


class TestSessionEndHook(unittest.TestCase):
    def test_absent_wiki_layer_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(se.sync_obsidian_vault({"wiki_dir": d}))

    def test_opt_out_is_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_wiki(Path(d))
            r = se.sync_obsidian_vault({"wiki_dir": d, "skip_vault_sync": True})
            self.assertEqual(r, {"skipped": True, "reason": "disabled"})

    def test_success_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_wiki(Path(d), 0)
            self.assertEqual(se.sync_obsidian_vault({"wiki_dir": d}),
                             {"returncode": 0})

    def test_failure_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_wiki(Path(d), 3)
            self.assertEqual(se.sync_obsidian_vault({"wiki_dir": d}),
                             {"returncode": 3})

    def test_env_var_locates_the_wiki(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_wiki(Path(d))
            old = os.environ.get("LTM_WIKI_DIR")
            os.environ["LTM_WIKI_DIR"] = d
            try:
                self.assertEqual(se.sync_obsidian_vault({}), {"returncode": 0})
            finally:
                os.environ.pop("LTM_WIKI_DIR", None)
                if old is not None:
                    os.environ["LTM_WIKI_DIR"] = old

    def test_never_raises_on_garbage_input(self):
        for bad in ({}, {"wiki_dir": None}, {"wiki_dir": 12345}):
            try:
                se.sync_obsidian_vault(bad)
            except Exception as e:                          # pragma: no cover
                self.fail(f"hook raised on {bad!r}: {e}")


@unittest.skipUnless((WIKI / "sync.py").is_file(), "wiki layer not present")
class TestSyncDriver(unittest.TestCase):
    """sync.py owns the marker and the lock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.vault = root / "vault"
        self.state = self.vault / ".wiki"
        self.env = dict(os.environ)
        self.env.update({
            "COPILOT_HOME": str(root / "home"),
            "LTM_WIKI_VAULT": str(self.vault),
            "LTM_MEMORY_DB": str(root / "home" / "memory.db"),   # absent on purpose
            "LTM_SESSION_DB": str(root / "home" / "session.db"),
            "LTM_WIKI_WORKSPACE": "",
            "PYTHONIOENCODING": "utf-8",
        })

    def tearDown(self):
        self._tmp.cleanup()

    def run_sync(self, *args):
        return subprocess.run([sys.executable, str(WIKI / "sync.py"), *args],
                              cwd=str(WIKI), env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=300)

    def test_mark_creates_the_marker(self):
        r = self.run_sync("--mark", "unit-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = self.state / "dirty"
        self.assertTrue(marker.is_file())
        self.assertIn("unit-test", marker.read_text(encoding="utf-8"))

    def test_marker_lives_with_the_vault_not_the_repo(self):
        self.run_sync("--mark", "x")
        self.assertTrue((self.state / "dirty").is_file())
        self.assertFalse((WIKI / "dirty").exists(),
                         "build state must not be written into the repo")

    def test_no_memory_database_is_not_an_error(self):
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no memory database", r.stdout.lower())

    def test_status_never_fails(self):
        self.assertEqual(self.run_sync("--status").returncode, 0)

    def test_lock_prevents_a_concurrent_run(self):
        # A held lock must make a second run stand down rather than race.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "dirty").write_text("x", encoding="utf-8")
        (self.state / "sync.lock").write_text("", encoding="utf-8")
        # Give it a database so it gets past the empty-mode check.
        db = Path(self.env["LTM_MEMORY_DB"])
        db.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(db).close()
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already running", r.stdout.lower())
        self.assertTrue((self.state / "dirty").is_file(),
                        "a skipped sync must leave the marker for the next run")


@unittest.skipUnless((WIKI / "vaultlib.py").is_file(), "wiki layer not present")
class TestIncrementalScoping(unittest.TestCase):
    """The incremental rebuild must only delete notes it will regenerate."""

    def setUp(self):
        sys.path.insert(0, str(WIKI))
        import vaultlib
        self.vaultlib = vaultlib
        self._real = vaultlib.VAULT
        self._tmp = tempfile.TemporaryDirectory()
        vaultlib.VAULT = Path(self._tmp.name)

    def tearDown(self):
        self.vaultlib.VAULT = self._real
        self._tmp.cleanup()

    def _note(self, name, *, generated=True, origin=None):
        p = self.vaultlib.VAULT / f"{name}.md"
        fm = ["---", f"title: {name}"]
        if generated:
            fm.append("generated: true")
        if origin:
            fm.append(f"origin: {origin}")
        fm.append("---")
        p.write_text("\n".join(fm) + "\n\nbody\n", encoding="utf-8")
        return p

    def test_memory_scope_spares_workspace_and_handwritten(self):
        mem = self._note("mem-note", origin="memory")
        ws = self._note("ws-note", origin="workspace")
        idx = self._note("idx-note", origin="index")
        hand = self._note("hand-note", generated=False)

        removed = self.vaultlib.clear_generated(origin="memory")

        self.assertEqual(removed, 1)
        self.assertFalse(mem.exists(), "memory note should be removed")
        self.assertTrue(ws.exists(), "workspace note must survive an incremental run")
        self.assertTrue(idx.exists(), "index note must survive an incremental run")
        self.assertTrue(hand.exists(), "hand-authored note must never be removed")

    def test_unstamped_generated_notes_are_treated_as_workspace(self):
        # Notes written before origin stamping existed carry no origin. Deleting
        # them during a memory-only run would lose content that run cannot rebuild.
        legacy = self._note("legacy-note")
        self.vaultlib.clear_generated(origin="memory")
        self.assertTrue(legacy.exists())

    def test_full_scope_removes_all_generated_but_not_handwritten(self):
        self._note("mem-note", origin="memory")
        self._note("ws-note", origin="workspace")
        hand = self._note("hand-note", generated=False)

        removed = self.vaultlib.clear_generated()

        self.assertEqual(removed, 2)
        self.assertTrue(hand.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
