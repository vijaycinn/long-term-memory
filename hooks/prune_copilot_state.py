#!/usr/bin/env python3
"""Session-end retention sweep for Copilot CLI state.

Two directories grow without bound and nothing prunes them:

* ``~/.copilot/logs/extensions/`` — one launch log per canvas-extension fork,
  per session. 1,674 files had accumulated by 2026-08-27. They are pure debug
  output and worthless once the session is over.
* ``~/.copilot/session-state/<uuid>/`` — 630 directories on the same date.
  These back ``copilot --resume``, so they are treated far more carefully.

Retention is deliberately lopsided. Extension logs go after a week because
nothing reads them. Session state is kept for two months because deleting it
destroys the ability to resume, and the cost of keeping it is only disk.

``session-store.db`` is left alone on purpose. It is the CLI's own SQLite
database, and pruning rows out from under a running CLI risks corruption for a
saving measured in tens of megabytes.

Usage::

    python prune_copilot_state.py --check   # report only, deletes nothing
    python prune_copilot_state.py           # sweep

Runs from the ``sessionEnd`` hook, never ``sessionStart`` — startup latency is
user-visible and is being kept at zero deliberately.

Input (stdin): JSON hook payload — unused.
Output: none in hook mode. ``sessionEnd`` hooks ignore stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXTENSION_LOG_RETENTION_DAYS = 7
SESSION_STATE_RETENTION_DAYS = 60

# Never delete state for a session that is still running, however old its
# directory looks. A live session writes continuously, so recent mtime is the
# signal; this is belt-and-braces on top of the retention window.
ACTIVE_SESSION_GRACE_SECONDS = 60 * 60


def copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".copilot"


def log(home: Path, message: str) -> None:
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (home / "hook-guard.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def newest_mtime(path: Path) -> float:
    """Most recent mtime anywhere under ``path``.

    A session directory's own mtime only changes when a child is added or
    removed, so a long session that keeps appending to ``events.jsonl`` can
    look stale at the top level. Walking finds the real last-touched time.
    """
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, (Path(root) / name).stat().st_mtime)
            except OSError:
                continue
    return newest


def sweep_extension_logs(home: Path, now: float, dry_run: bool) -> tuple[int, int]:
    directory = home / "logs" / "extensions"
    if not directory.is_dir():
        return 0, 0
    cutoff = now - EXTENSION_LOG_RETENTION_DAYS * 86400
    count = 0
    freed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        count += 1
        freed += stat.st_size
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                count -= 1
                freed -= stat.st_size
    return count, freed


def sweep_session_state(home: Path, now: float, dry_run: bool) -> tuple[int, int]:
    directory = home / "session-state"
    if not directory.is_dir():
        return 0, 0
    cutoff = now - SESSION_STATE_RETENTION_DAYS * 86400
    active_cutoff = now - ACTIVE_SESSION_GRACE_SECONDS
    current = os.environ.get("COPILOT_SESSION_ID")
    count = 0
    freed = 0
    for path in directory.iterdir():
        if not path.is_dir() or path.name == current:
            continue
        touched = newest_mtime(path)
        if touched >= cutoff or touched >= active_cutoff:
            continue
        size = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    size += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
        count += 1
        freed += size
    return count, freed


def mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def main() -> int:
    dry_run = any(a in ("--check", "--dry-run", "-n") for a in sys.argv[1:])

    if not dry_run:
        try:
            sys.stdin.read()
        except Exception:
            pass

    home = copilot_home()
    now = time.time()

    logs_n, logs_bytes = sweep_extension_logs(home, now, dry_run)
    state_n, state_bytes = sweep_session_state(home, now, dry_run)

    if dry_run:
        print(f"copilot home: {home}")
        print(
            f"extension logs older than {EXTENSION_LOG_RETENTION_DAYS}d: "
            f"{logs_n} file(s), {mb(logs_bytes)}"
        )
        print(
            f"session state older than {SESSION_STATE_RETENTION_DAYS}d: "
            f"{state_n} dir(s), {mb(state_bytes)}"
        )
        if not (logs_n or state_n):
            print("nothing to sweep")
        return 0

    if logs_n:
        log(home, f"SWEPT {logs_n} extension log(s), {mb(logs_bytes)}")
    if state_n:
        log(home, f"SWEPT {state_n} session-state dir(s), {mb(state_bytes)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: a retention sweep must never break the session.
        sys.exit(0)
