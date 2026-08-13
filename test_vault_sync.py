"""Tests for the Obsidian vault sync wired into LTM persistence.

Two things are guarded here:

1. `sync_obsidian_vault` in the sessionEnd hook — it must never raise, must
   honour the lock and the opt-out, and must clear the dirty marker only on
   success.
2. `clear_generated(origin=...)` in the vault builder — the incremental rebuild
   must delete only the notes it owns. This regressed once already, silently
   dropping imported account documents that happened to live in a folder the
   builder treated as memory-derived.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VAULT_BUILD = Path(r"C:\workspace\agency\obsidian\_build")

import ltm_session_end as se


class TestSessionEndVaultSync(unittest.TestCase):
    def test_missing_builder_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(se.sync_obsidian_vault({"vault_build_dir": d}))

    def test_opt_out_is_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "build.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
            r = se.sync_obsidian_vault({"vault_build_dir": d, "skip_vault_sync": True})
            self.assertEqual(r, {"skipped": True, "reason": "disabled"})

    def test_lock_prevents_concurrent_run(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "build.py").write_text("raise SystemExit(0)", encoding="utf-8")
            Path(d, ".vault-sync.lock").write_text("", encoding="utf-8")
            r = se.sync_obsidian_vault({"vault_build_dir": d})
            self.assertEqual(r, {"skipped": True, "reason": "locked"})

    def test_success_clears_dirty_marker_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "build.py").write_text("raise SystemExit(0)", encoding="utf-8")
            dirty = Path(d, ".vault-dirty")
            dirty.write_text("x", encoding="utf-8")
            r = se.sync_obsidian_vault({"vault_build_dir": d})
            self.assertEqual(r["returncode"], 0)
            self.assertFalse(dirty.exists(), "dirty marker should be cleared on success")
            self.assertFalse(Path(d, ".vault-sync.lock").exists(),
                             "lock must be released")

    def test_failure_leaves_dirty_marker(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "build.py").write_text("raise SystemExit(3)", encoding="utf-8")
            dirty = Path(d, ".vault-dirty")
            dirty.write_text("x", encoding="utf-8")
            r = se.sync_obsidian_vault({"vault_build_dir": d})
            self.assertEqual(r["returncode"], 3)
            self.assertTrue(dirty.exists(),
                            "a failed sync must stay dirty so the next run retries")

    def test_env_var_selects_builder_location(self):
        # The default path is machine-specific; a clone on another machine must
        # be able to point the hook elsewhere without editing source.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "build.py").write_text("raise SystemExit(0)", encoding="utf-8")
            old = os.environ.get("LTM_VAULT_BUILD_DIR")
            os.environ["LTM_VAULT_BUILD_DIR"] = d
            try:
                r = se.sync_obsidian_vault({})
            finally:
                if old is None:
                    os.environ.pop("LTM_VAULT_BUILD_DIR", None)
                else:
                    os.environ["LTM_VAULT_BUILD_DIR"] = old
            self.assertEqual(r, {"returncode": 0})

    def test_hook_input_overrides_env_var(self):
        with tempfile.TemporaryDirectory() as good, \
                tempfile.TemporaryDirectory() as ignored:
            Path(good, "build.py").write_text("raise SystemExit(0)", encoding="utf-8")
            old = os.environ.get("LTM_VAULT_BUILD_DIR")
            os.environ["LTM_VAULT_BUILD_DIR"] = ignored
            try:
                r = se.sync_obsidian_vault({"vault_build_dir": good})
            finally:
                if old is None:
                    os.environ.pop("LTM_VAULT_BUILD_DIR", None)
                else:
                    os.environ["LTM_VAULT_BUILD_DIR"] = old
            self.assertEqual(r, {"returncode": 0})

    def test_never_raises_on_garbage_input(self):
        for bad in ({}, {"vault_build_dir": None}, {"vault_build_dir": 12345}):
            try:
                se.sync_obsidian_vault(bad)
            except Exception as e:                      # pragma: no cover
                self.fail(f"hook raised on {bad!r}: {e}")


@unittest.skipUnless(VAULT_BUILD.is_dir(), "vault builder not present")
class TestIncrementalScoping(unittest.TestCase):
    """The incremental rebuild must only delete notes it will regenerate."""

    def setUp(self):
        sys.path.insert(0, str(VAULT_BUILD))
        import vaultlib
        self.vaultlib = vaultlib
        self._real_vault = vaultlib.VAULT
        self._tmp = tempfile.TemporaryDirectory()
        vaultlib.VAULT = Path(self._tmp.name)

    def tearDown(self):
        self.vaultlib.VAULT = self._real_vault
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
