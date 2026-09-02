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
# Repository is organised by function; nothing is installed as a package, so the
# sibling folders holding the modules under test go on the path explicitly.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sib in ("core", "hooks", "ops", "wiki"):
    _p = _os.path.join(_REPO, _sib)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The repository is organised by function, so the wiki layer is a sibling of
# tests/, not a child of it. Getting this wrong is quiet rather than loud: the
# skipUnless guards below simply skip, and a green run hides eight dead tests.
REPO = HERE.parent
WIKI = REPO / "wiki"
SCHEMA_SQL = REPO / "setup" / "init-memory.sql"
sys.path.insert(0, str(HERE))

import ltm_session_end as se


def _seed_memory_db(path: Path) -> None:
    """A minimal but real memory.db -- schema plus one topic/fact -- so
    build.py --memory-only has something to render into an actual page. This
    lets the vector-index tests below exercise a real page rebuild rather than
    short-circuiting on the earlier `mode == "empty"` check."""
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.execute("INSERT INTO topics (slug, title, category, status) "
                    "VALUES ('t1', 'Test Topic', 'research', 'active')")
        con.execute(
            "INSERT INTO facts (topic_id, content, fact_type, importance, status) "
            "VALUES (1, 'A durable test fact for the vector index proof', "
            "'finding', 3, 'active')")
        con.commit()
    finally:
        con.close()


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


@unittest.skipUnless((WIKI / "vault_vectors.py").is_file(), "wiki layer not present")
class TestDetectContextualMode(unittest.TestCase):
    """The correctness trap this task exists to close: vault_vectors records
    `dimensions` and `model` in its meta table but never which chunking mode
    built the sidecar. The only surviving signal is content_hash, since the
    header text is hashed along with the chunk in contextual mode. Prove the
    inference lands right using vault_vectors's own real chunking/hash code --
    never mocked, because that logic must not lie to us -- against a hand-built
    `chunks` table standing in for the sidecar.
    """

    def setUp(self):
        sys.path.insert(0, str(WIKI))
        import sync as _sync
        import vault_vectors as vv
        self.sync = _sync
        self.vv = vv
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()
        self.db = Path(self._tmp.name) / "vectors.db"
        (self.vault / "page.md").write_text(
            "# A real page\n\nEnough body text here to form one real chunk "
            "worth hashing for the mode-detection fixture.\n",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_chunk_row(self, path: str, chunk_ix: int, content_hash: str) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS chunks "
                       "(path TEXT, chunk_ix INTEGER, content_hash TEXT)")
            con.execute("INSERT INTO chunks (path, chunk_ix, content_hash) "
                       "VALUES (?,?,?)", (path, chunk_ix, content_hash))
            con.commit()
        finally:
            con.close()

    def _hash_for(self, contextual: bool) -> str:
        raw = (self.vault / "page.md").read_text(encoding="utf-8")
        pieces = self.vv._prepared_chunks("page.md", raw, contextual)
        return self.vv._hash(pieces[0][1])

    def test_detects_contextual_when_hash_matches_header_form(self):
        self._write_chunk_row("page.md", 0, self._hash_for(True))
        mode, status, _ = self.sync._detect_contextual_mode(self.vault, self.db, self.vv)
        self.assertEqual((mode, status), (True, "detected"))

    def test_detects_plain_when_hash_matches_bare_form(self):
        self._write_chunk_row("page.md", 0, self._hash_for(False))
        mode, status, _ = self.sync._detect_contextual_mode(self.vault, self.db, self.vv)
        self.assertEqual((mode, status), (False, "detected"))

    def test_fresh_when_sidecar_has_no_chunks_yet(self):
        # db file does not even exist yet -- nothing to sample, nothing to
        # match. This is the only case where the caller is allowed to default.
        mode, status, _ = self.sync._detect_contextual_mode(self.vault, self.db, self.vv)
        self.assertEqual(status, "fresh")
        self.assertIsNone(mode)

    def test_ambiguous_when_stored_hash_matches_neither_mode(self):
        # Stands in for a page that changed since the sidecar was built: the
        # caller must fail loud here rather than guess and risk writing chunks
        # in the wrong mode.
        self._write_chunk_row("page.md", 0, "0" * 32)
        mode, status, _ = self.sync._detect_contextual_mode(self.vault, self.db, self.vv)
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(mode)


@unittest.skipUnless((WIKI / "vault_vectors.py").is_file(), "wiki layer not present")
class TestVectorIndexDispatch(unittest.TestCase):
    """`_sync_vector_index` in-process, with `_run_vector_index_subprocess`
    stubbed so these never need sqlite-vec or a live embedding endpoint. This
    is the seam that matters most: the historical bug was `--limit-pages`
    silently truncating out-of-scope chunks, so proving sync.py never
    constructs that flag is worth more here than any amount of mocking of
    vault_vectors's own (already-tested) internals.
    """

    def setUp(self):
        sys.path.insert(0, str(WIKI))
        import sync as _sync
        import vault_vectors as vv
        self.sync = _sync
        self.vv = vv
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()
        self.db = Path(self._tmp.name) / "vectors.db"     # deliberately absent
        self._saved_run = _sync._run_vector_index_subprocess
        self._saved_available = vv.available
        self._saved_autoindex = os.environ.get("LTM_VAULT_AUTOINDEX")

    def tearDown(self):
        self.sync._run_vector_index_subprocess = self._saved_run
        self.vv.available = self._saved_available
        if self._saved_autoindex is None:
            os.environ.pop("LTM_VAULT_AUTOINDEX", None)
        else:
            os.environ["LTM_VAULT_AUTOINDEX"] = self._saved_autoindex
        self._tmp.cleanup()

    def _stub_subprocess(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        calls = []

        def fake(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        self.sync._run_vector_index_subprocess = fake
        return calls

    def test_explicit_opt_out_never_touches_vault_vectors(self):
        # If this reaches vault_vectors at all the test fails loudly, rather
        # than just asserting zero subprocess calls -- an opt-out must be
        # checked *first*, before any availability probe.
        self.vv.available = lambda: (_ for _ in ()).throw(
            AssertionError("opt-out must short-circuit before availability check"))
        calls = self._stub_subprocess()
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=True)
        self.assertEqual(calls, [])

    def test_env_var_opt_out_is_honoured(self):
        os.environ["LTM_VAULT_AUTOINDEX"] = "0"
        self.vv.available = lambda: (_ for _ in ()).throw(
            AssertionError("env-var opt-out must short-circuit too"))
        calls = self._stub_subprocess()
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=False)
        self.assertEqual(calls, [])

    def test_unavailable_embeddings_skip_without_spawning(self):
        self.vv.available = lambda: False
        calls = self._stub_subprocess()
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=False)
        self.assertEqual(calls, [])

    def test_never_passes_limit_pages(self):
        self.vv.available = lambda: True
        calls = self._stub_subprocess(
            stdout="embedded 1, reused 0, deleted 0 across 1 pages")
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--limit-pages", calls[0])

    def test_fresh_sidecar_defaults_to_contextual(self):
        # No existing chunks to match -- this is the one case allowed to
        # default, and it must default to the mode already promoted live.
        self.vv.available = lambda: True
        calls = self._stub_subprocess(
            stdout="embedded 0, reused 0, deleted 0 across 0 pages")
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=False)
        self.assertIn("--contextual", calls[0])

    def test_ambiguous_mode_skips_without_spawning(self):
        # A sidecar that already disagrees with itself must not be written to
        # further -- confirm no subprocess is spawned at all.
        self.vv.available = lambda: True
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE chunks (path TEXT, chunk_ix INTEGER, content_hash TEXT)")
        con.execute("INSERT INTO chunks VALUES ('missing-page.md', 0, 'deadbeef')")
        con.commit()
        con.close()
        calls = self._stub_subprocess()
        self.sync._sync_vector_index(self.vault, self.db, quiet=True, no_index=False)
        self.assertEqual(calls, [])


@unittest.skipUnless((WIKI / "vault_vectors.py").is_file(), "wiki layer not present")
class TestVectorIndexCliWiring(unittest.TestCase):
    """End-to-end through the real sync.py CLI against a real (tiny) memory.db,
    with LTM_EMBED_ENDPOINT deliberately left unset so vault_vectors.available()
    genuinely returns False -- this exercises the real degrade-cleanly path
    rather than a mocked stand-in for it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.vault = root / "vault"
        self.home.mkdir(parents=True)
        self.memdb = self.home / "memory.db"
        _seed_memory_db(self.memdb)
        self.env = dict(os.environ)
        self.env.pop("LTM_EMBED_ENDPOINT", None)
        self.env.pop("LTM_VAULT_AUTOINDEX", None)
        self.env.update({
            "COPILOT_HOME": str(self.home),
            "LTM_WIKI_VAULT": str(self.vault),
            "LTM_MEMORY_DB": str(self.memdb),
            "LTM_SESSION_DB": str(self.home / "session.db"),
            "LTM_WIKI_WORKSPACE": "",
            "PYTHONIOENCODING": "utf-8",
        })

    def tearDown(self):
        self._tmp.cleanup()

    def run_sync(self, *args, env=None):
        return subprocess.run([sys.executable, str(WIKI / "sync.py"), *args],
                              cwd=str(WIKI), env=(env or self.env),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def test_status_never_touches_the_vector_step(self):
        self.run_sync("--mark", "seed")
        r = self.run_sync("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("vault_vectors", r.stdout)

    def test_noop_sync_never_touches_the_vector_step(self):
        r = self.run_sync()          # no marker: must stop at "already current"
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already current", r.stdout.lower())
        self.assertNotIn("vault_vectors", r.stdout)

    def test_real_build_then_graceful_visible_skip_when_unavailable(self):
        self.run_sync("--mark", "seed")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wiki synced", r.stdout.lower())
        self.assertIn("[vault_vectors] index skipped", r.stdout)
        self.assertIn("az login", r.stdout)

    def test_explicit_no_index_flag_is_visible(self):
        self.run_sync("--mark", "seed")
        r = self.run_sync("--no-index")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-index", r.stdout)

    def test_env_var_opt_out_is_honoured_through_the_cli(self):
        self.run_sync("--mark", "seed")
        env = dict(self.env)
        env["LTM_VAULT_AUTOINDEX"] = "0"
        r = self.run_sync(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LTM_VAULT_AUTOINDEX", r.stdout)

    def test_quiet_suppresses_the_vector_message_too(self):
        self.run_sync("--mark", "seed")
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
