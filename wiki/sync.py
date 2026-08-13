"""sync.py — keep the wiki level with memory, cheaply.

Rebuilding costs seconds, so a memory write must not pay for it. Writes only
drop a marker; the rebuild happens at a natural boundary (context export,
matrix-load, session end) or on demand.

    python sync.py --mark "Add-Fact"   # instant, just records that memory moved
    python sync.py --status            # what would happen, changes nothing
    python sync.py                     # incremental rebuild if marked
    python sync.py --full              # also rescan the source workspace
    python sync.py --force             # rebuild even when nothing is marked

Concurrency is handled with an exclusive lock file, so a background sync cannot
collide with a manual one. A failed sync leaves the marker in place, so the next
run retries rather than silently dropping the change.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402


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
    dirty = (d / "dirty")
    print(config.describe())
    print(f"dirty         : {'yes' if dirty.is_file() else 'no'}"
          + (f" ({len(dirty.read_text(encoding='utf-8').splitlines())} write(s) pending)"
             if dirty.is_file() else ""))
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


def cmd_sync(full: bool, force: bool, quiet: bool) -> int:
    p = config.paths()
    if p["mode"] == "empty":
        if not quiet:
            print("nothing to build: no memory database found "
                  f"({p['memory_db']})")
        return 0

    d = state_dir()
    dirty = d / "dirty"
    if not dirty.is_file() and not force and not full:
        if not quiet:
            print("wiki already current (no memory writes since last sync)")
        return 0

    d.mkdir(parents=True, exist_ok=True)
    lock = d / "sync.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not quiet:
            print("a sync is already running - skipping")
        return 0

    started = datetime.now(timezone.utc)
    try:
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
            return proc.returncode        # marker deliberately left in place
        dirty.unlink(missing_ok=True)
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        pages = ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith("[index]"):
                pages = line.split("catalogued", 1)[-1].strip()
        if not quiet:
            print(f"wiki synced ({'full' if full else 'incremental'})"
                  + (f" - {pages}" if pages else "") + f" in {secs:.1f}s")
        return 0
    finally:
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
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if a.mark is not None:
        return cmd_mark(a.mark)
    if a.status:
        return cmd_status()
    return cmd_sync(a.full, a.force, a.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
