"""sync.py — keep the wiki level with memory, cheaply.

Rebuilding costs seconds, so a memory write must not pay for it. Writes only
drop a marker; the rebuild happens at a natural boundary (context export,
matrix-load, session end) or on demand.

    python sync.py --mark "Add-Fact"   # instant, just records that memory moved
    python sync.py --status            # what would happen, changes nothing
    python sync.py                     # incremental rebuild if marked
    python sync.py --full              # also rescan the source workspace
    python sync.py --force             # rebuild even when nothing is marked
    python sync.py --no-index          # rebuild pages but skip the vector step

Concurrency is handled with an exclusive lock file, so a background sync cannot
collide with a manual one. A failed sync leaves the marker in place, so the next
run retries rather than silently dropping the change.

After a successful page rebuild, changed pages are also re-embedded into the
vector sidecar (wiki/vault_vectors.py) -- otherwise a captured fact is
keyword-findable immediately but semantically invisible until someone
remembers to re-index by hand. That gap is exactly why this step runs here
rather than nowhere: Sync-Vault is already the "make the wiki current" verb,
it is user-invoked so a failure is seen rather than swallowed at session end,
and vault_vectors's own content-hash invalidation means a normal sync only
re-embeds a handful of pages, not the whole corpus. Disable with --no-index or
$env:LTM_VAULT_AUTOINDEX = '0'; default is on.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

# index() commits every 64-chunk batch (see vault_vectors.py), so a kill on
# timeout only loses the in-flight batch -- never previously committed rows.
# A fresh multi-thousand-page vault is the only realistic way to approach this
# ceiling; a normal incremental sync (a handful of changed pages) finishes in
# seconds.
VECTOR_INDEX_TIMEOUT = 3600


def state_dir() -> Path:
    return config.paths()["vault"] / ".wiki"


def cmd_mark(reason: str) -> int:
    d = state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        with (d / "dirty").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()}\t{reason}\n")
    except OSError as e:
        print(f"could not mark: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_status() -> int:
    p = config.paths()
    d = state_dir()
    dirty = d / "dirty"
    processing = d / "dirty.processing"
    pending = 0
    for marker in (dirty, processing):
        if marker.is_file():
            try:
                pending += len(marker.read_text(encoding="utf-8").splitlines())
            except OSError:
                pending += 1
    print(config.describe())
    print(f"dirty         : {'yes' if pending else 'no'}"
          + (f" ({pending} write(s) pending)" if pending else ""))
    print(f"lock held     : {'yes' if (d / 'sync.lock').is_file() else 'no'}")
    last = d / "last-build.json"
    if last.is_file():
        try:
            info = json.loads(last.read_text(encoding="utf-8"))
            print(f"last build    : {info.get('stamp')} ({info.get('mode')}, "
                  f"{info.get('notes')} notes)")
        except (json.JSONDecodeError, OSError):
            pass
    if p["mode"] == "empty":
        print("\nNo memory database yet. Run install-wiki.ps1, or create memory.db "
              "with the LTM installer first.")
    return 0


def _index_opt_out(explicit_flag: bool) -> bool:
    """--no-index always wins; otherwise LTM_VAULT_AUTOINDEX=0 disables the
    default-on step. Inverted from LTM_VAULT_AUTOSYNC (that one is opt-IN and
    defaults off) because the entire point of wiring this in is that it must
    run without anyone remembering to ask -- that is the gap it closes."""
    if explicit_flag:
        return True
    return os.environ.get("LTM_VAULT_AUTOINDEX", "1").strip() == "0"


def _run_vector_index_subprocess(argv: list[str], timeout: int):
    """Thin seam around subprocess.run so tests can substitute a stub instead
    of spawning a process that needs sqlite-vec and a live embedding endpoint."""
    return subprocess.run(argv, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _sample_chunk_rows(db: Path, limit: int = 5) -> list[tuple[str, int, str]]:
    """A handful of (path, chunk_ix, content_hash) rows from the sidecar.

    Deliberately plain sqlite3 against the ordinary `chunks` table, never the
    vec0 virtual table -- this works even without sqlite-vec loaded, so mode
    detection cannot itself be the thing that fails when embeddings are
    unavailable. Read-only URI connection: this must never be the process that
    corrupts or locks the sidecar.
    """
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT path, chunk_ix, content_hash FROM chunks "
                "WHERE chunk_ix = 0 ORDER BY path LIMIT ?", (limit,)
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _detect_contextual_mode(vault: Path, db: Path, vv) -> tuple[bool | None, str, str]:
    """Infer whether the live sidecar was built with contextual chunk headers.

    vault_vectors records `dimensions` and `model` in its meta table but never
    the chunking mode. The only surviving signal is content_hash: it is hashed
    over the *indexed* text, which includes the context header when contextual
    chunking was used, so the hash itself differs between modes. Recompute
    both hashes for a few still-live pages and see which one the stored hash
    matches. A page that changed since indexing gives no evidence either way
    and is simply skipped, not counted as a vote.

    Returns (contextual, status, detail):
      status "detected"  -- contextual is authoritative
      status "fresh"      -- no existing chunks to sample; caller may default
      status "ambiguous"  -- existing evidence conflicts or is absent; the
                             caller MUST NOT default here, because writing the
                             wrong mode would silently mix contextual and
                             non-contextual hashes across the whole corpus.
    """
    rows = _sample_chunk_rows(db)
    if not rows:
        return None, "fresh", "no existing sidecar chunks to sample"

    votes: set[bool] = set()
    inspected = 0
    for path, chunk_ix, content_hash in rows:
        page = vault / path
        if not page.is_file():
            continue
        raw = vv._read(page)
        if not raw:
            continue
        inspected += 1
        for candidate in (True, False):
            pieces = vv._prepared_chunks(path, raw, candidate)
            if chunk_ix >= len(pieces):
                continue
            _, indexed_text = pieces[chunk_ix]
            if vv._hash(indexed_text) == content_hash:
                votes.add(candidate)

    if len(votes) == 1:
        return votes.pop(), "detected", f"detected from {inspected} sampled page(s)"
    if not votes:
        return None, "ambiguous", f"none of {inspected} sampled page(s) matched either mode"
    return None, "ambiguous", "sampled pages matched both modes -- sidecar may already be mixed"


_SUMMARY_RE = re.compile(r"embedded (\d+), reused (\d+), deleted (\d+) across (\d+) pages")


def _parse_index_summary(stdout: str) -> str | None:
    m = _SUMMARY_RE.search(stdout or "")
    if not m:
        return None
    embedded, reused, deleted, pages = m.groups()
    return (f"embedded {embedded}, reused {reused} unchanged, deleted {deleted}, "
            f"across {pages} pages")


def _explain_index_failure(stderr: str) -> str:
    """Turn vault_vectors's own stderr into one plain-English line.

    vault_vectors already prints a specific reason for every failure mode it
    knows about (sqlite-vec absent, no endpoint, dimension mismatch, az login).
    This just picks the recognisable ones out so Sync-Vault's own output stays
    short; the full text is still in stderr for anyone who needs it.
    """
    text = stderr or ""
    if "az account get-access-token failed" in text or "could not run az" in text:
        return "the Azure login backing embeddings has lapsed -- run `az login`"
    if "sqlite-vec not installed" in text:
        return "sqlite-vec is not installed"
    if "no embed_endpoint configured" in text:
        return "no embed_endpoint is configured"
    if "dimensions but config asks for" in text:
        return "the sidecar's embedding dimensions no longer match the config"
    if "embedding failed at batch" in text:
        return "the embedding endpoint failed mid-run (check `az login` / network)"
    excerpt = text.strip().splitlines()[-1] if text.strip() else "no diagnostic output"
    return f"unexpected failure -- {excerpt[:200]}"


def _restore_processing_marker(processing: Path, dirty: Path) -> None:
    """Return the captured marker to the pending queue after a failed sync."""
    if not processing.is_file():
        return
    try:
        content = processing.read_text(encoding="utf-8")
        if content:
            with dirty.open("a", encoding="utf-8") as fh:
                fh.write(content)
        processing.unlink(missing_ok=True)
    except OSError:
        # Leaving dirty.processing in place is safe: status reports it and the
        # next sync retries it rather than silently declaring the vault current.
        pass


def _claim_dirty_marker(dirty: Path, processing: Path) -> None:
    """Move the current write set aside so later writes remain independently pending."""
    if processing.is_file():
        _restore_processing_marker(processing, dirty)
    if dirty.is_file():
        os.replace(dirty, processing)


def _sync_vector_index(vault: Path, db: Path, quiet: bool, no_index: bool) -> dict:
    """Embed vault pages changed by the rebuild that just completed.

    Called from inside cmd_sync's own lock, after its build succeeds, so this
    can never race a second sync's indexer against the same sqlite file.
    Returns an explicit state so callers can distinguish current, disabled,
    unavailable and failed semantic indexing. Only configured attempts that
    fail are errors; intentional opt-out and unavailable configuration leave
    the freshly rebuilt lexical pages current.
    """
    if _index_opt_out(no_index):
        if not quiet:
            print("[vault_vectors] index skipped (--no-index / LTM_VAULT_AUTOINDEX=0)")
        return {
            "state": "disabled",
            "configured": None,
            "current": False,
            "reason": "explicit opt-out",
        }

    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import vault_vectors as vv
    except ImportError as e:
        if not quiet:
            print(f"[vault_vectors] vector layer unavailable ({e}); pages are "
                  "current but not re-embedded", file=sys.stderr)
        return {
            "state": "unavailable",
            "configured": bool(config.get("embed_endpoint")),
            "current": False,
            "reason": f"vector module unavailable: {e}",
        }

    if not vv.available():
        sqlite_vec = vv._sqlite_vec() is not None
        endpoint = bool(config.get("embed_endpoint"))
        reasons = []
        if not sqlite_vec:
            reasons.append("sqlite-vec not installed")
        if not endpoint:
            reasons.append("no embed_endpoint configured")
        reason = " and ".join(reasons) or "vector layer unavailable"
        if not quiet:
            print(f"[vault_vectors] index skipped: {reason}. Check with "
                  "`python wiki\\vault_vectors.py status`; if that looks "
                  "configured, try `az login`.")
        return {
            "state": "unavailable",
            "configured": sqlite_vec and endpoint,
            "current": False,
            "reason": reason,
        }

    contextual, mode_status, mode_detail = _detect_contextual_mode(vault, db, vv)
    if mode_status == "ambiguous":
        if not quiet:
            print(f"[vault_vectors] index skipped: cannot tell whether the live "
                  f"sidecar uses contextual chunking ({mode_detail}). Writing "
                  f"chunks in the wrong mode would silently mix contextual and "
                  f"non-contextual hashes. Resolve manually with "
                  f"`python wiki\\vault_vectors.py index --contextual` (or "
                  f"without) after confirming the mode via "
                  f"`python wiki\\vault_vectors.py status`.", file=sys.stderr)
        return {
            "state": "failed",
            "configured": True,
            "current": False,
            "reason": f"contextual mode ambiguous: {mode_detail}",
        }
    if mode_status == "fresh":
        contextual = True   # nothing to match yet; this is the promoted mode

    before = vv.status(db)
    argv = [sys.executable, str(HERE / "vault_vectors.py"), "index", "--quiet"]
    if contextual:
        argv.append("--contextual")

    started = datetime.now(timezone.utc)
    try:
        proc = _run_vector_index_subprocess(argv, VECTOR_INDEX_TIMEOUT)
    except subprocess.TimeoutExpired:
        after = vv.status(db)
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        if not quiet:
            print(f"[vault_vectors] index timed out after {secs:.0f}s "
                  f"({after['chunks']} chunks / {after['pages']} pages now "
                  f"committed, up from {before['chunks']}/{before['pages']}). "
                  f"Batches are committed as they land, so nothing already "
                  f"embedded is lost -- re-run Sync-Vault to pick up where it "
                  f"left off.", file=sys.stderr)
        partial = (after["chunks"], after["pages"]) != (before["chunks"], before["pages"])
        return {
            "state": "partial" if partial else "failed",
            "configured": True,
            "current": False,
            "reason": f"index timed out after {secs:.0f}s",
            "before": before,
            "after": after,
        }

    secs = (datetime.now(timezone.utc) - started).total_seconds()
    if proc.returncode != 0:
        after = vv.status(db)
        reason = _explain_index_failure(proc.stderr)
        if not quiet:
            print(f"[vault_vectors] index failed: {reason}",
                  file=sys.stderr)
        partial = (after["chunks"], after["pages"]) != (before["chunks"], before["pages"])
        return {
            "state": "partial" if partial else "failed",
            "configured": True,
            "current": False,
            "reason": reason,
            "before": before,
            "after": after,
        }

    summary = _parse_index_summary(proc.stdout) or "index completed"
    if not quiet:
        print(f"[vault_vectors] {summary} in {secs:.1f}s")
    return {
        "state": "indexed",
        "configured": True,
        "current": True,
        "summary": summary,
        "seconds": round(secs, 3),
    }


def cmd_sync(full: bool, force: bool, quiet: bool, no_index: bool = False) -> dict:
    p = config.paths()
    if p["mode"] == "empty":
        if not quiet:
            print("nothing to build: no memory database found "
                  f"({p['memory_db']})")
        return {
            "ok": True,
            "exit_code": 0,
            "pages": {"state": "unavailable", "current": False,
                      "reason": "no memory database"},
            "index": {"state": "not-run", "configured": False, "current": False},
        }

    d = state_dir()
    dirty = d / "dirty"
    processing = d / "dirty.processing"
    if not dirty.is_file() and not processing.is_file() and not force and not full:
        if not quiet:
            print("wiki already current (no memory writes since last sync)")
        return {
            "ok": True,
            "exit_code": 0,
            "pages": {"state": "current", "current": True,
                      "mode": "incremental"},
            "index": {"state": "not-run", "configured": None, "current": None,
                      "reason": "no pending memory writes"},
        }

    d.mkdir(parents=True, exist_ok=True)
    lock = d / "sync.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not quiet:
            print("a sync is already running - skipping")
        return {
            "ok": False,
            "exit_code": 0,
            "pages": {"state": "busy", "current": False,
                      "mode": "full" if full else "incremental"},
            "index": {"state": "not-run", "configured": None, "current": False},
        }

    # Rotate the current write set after taking the sync lock. New memory writes
    # create a fresh `dirty` marker while this build runs, so completing this
    # sync can never erase work that arrived concurrently.
    started = datetime.now(timezone.utc)
    complete = False
    try:
        _claim_dirty_marker(dirty, processing)
        args = [sys.executable, str(HERE / "build.py")]
        if not full:
            args.append("--memory-only")
        proc = subprocess.run(args, cwd=str(HERE), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=1800)
        if proc.returncode != 0:
            log = d / "sync-error.log"
            log.write_text((proc.stdout or "") + "\n" + (proc.stderr or ""),
                           encoding="utf-8")
            print(f"sync FAILED (exit {proc.returncode}); see {log}", file=sys.stderr)
            return {
                "ok": False,
                "exit_code": proc.returncode,
                "pages": {"state": "failed", "current": False,
                          "mode": "full" if full else "incremental",
                          "error_log": str(log)},
                "index": {"state": "not-run", "configured": None, "current": False},
            }                              # marker deliberately left in place
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        pages = ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith("[index]"):
                pages = line.split("catalogued", 1)[-1].strip()
        if not quiet:
            print(f"wiki synced ({'full' if full else 'incremental'})"
                  + (f" - {pages}" if pages else "") + f" in {secs:.1f}s")
        # Still inside the lock: a second sync cannot start its own indexer
        # against the same sqlite file while this one is embedding.
        index_result = _sync_vector_index(
            p["vault"], p["vault_vectors"], quiet, no_index)
        if index_result["state"] in {"failed", "partial"}:
            return {
                "ok": False,
                "exit_code": 4,
                "pages": {"state": "rebuilt", "current": True,
                          "mode": "full" if full else "incremental",
                          "seconds": round(secs, 3)},
                "index": index_result,
            }                              # marker retained so the index retries
        processing.unlink(missing_ok=True)
        complete = True
        return {
            "ok": True,
            "exit_code": 0,
            "pages": {"state": "rebuilt", "current": True,
                      "mode": "full" if full else "incremental",
                      "seconds": round(secs, 3)},
            "index": index_result,
        }
    finally:
        if not complete:
            _restore_processing_marker(processing, dirty)
        os.close(fd)
        lock.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mark", metavar="REASON", nargs="?", const="memory write",
                    help="record that memory changed; does not rebuild")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--full", action="store_true", help="also rescan the workspace")
    ap.add_argument("--force", action="store_true", help="rebuild even when clean")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-index", action="store_true",
                    help="skip the vector-index step (or set LTM_VAULT_AUTOINDEX=0)")
    ap.add_argument("--json", action="store_true",
                    help="emit a final machine-readable sync result")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if a.mark is not None:
        return cmd_mark(a.mark)
    if a.status:
        return cmd_status()
    result = cmd_sync(a.full, a.force, a.quiet, a.no_index)
    if a.json:
        print(json.dumps(result, default=str))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
