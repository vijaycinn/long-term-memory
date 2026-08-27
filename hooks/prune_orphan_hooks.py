#!/usr/bin/env python3
"""Session-end guard: prune orphaned Copilot CLI hook configurations.

Runs at ``sessionEnd``, deliberately. ``sessionStart`` is kept empty because
startup latency is user-visible and three hooks were removed from it on
2026-08-27 to reclaim exactly that time. Pruning at shutdown costs nothing a
user waits on and still clears the config before the next session loads it.

Also runnable by hand:

    python prune_orphan_hooks.py --check   # report only, writes nothing
    python prune_orphan_hooks.py           # prune

Background
----------
Copilot CLI loads every ``*.json`` under ``~/.copilot/hooks/``. A config left
behind by an uninstalled or upgraded tool keeps firing on every turn. When its
target is gone, the CLI can fail the hook dispatch outright:

    Execution failed: GenericFailure, Hook processor is not configured for
    session id: <id>

That is what a stale Agency ``agency.json`` from 2026-05-12 did: it invoked
``node %APPDATA%/agency/hooks/copilot/agency-hook.cjs`` against a hardcoded
``127.0.0.1:7824`` that no longer had a listener. Agency's own cleanup could
not see it, because both ``remove_copilot_hooks_at`` and
``cleanup_stale_copilot_hooks_at`` locate a config by the loopback URL embedded
in the hook *command string*, and the legacy Node form hides its URL inside the
``.cjs`` file. The config was invisible to its own uninstaller and survived
every upgrade.

This guard closes that gap generically: an entry is orphaned when the thing it
invokes demonstrably no longer exists.

Two independent signals, both requiring hard evidence:

1. The command references an absolute script path that is not on disk.
2. The command posts to a loopback port with no listener.

Safety
------
The port probe races a daemon that is still coming up, so it only applies to a
config older than ``FRESH_CONFIG_GRACE`` — a live daemon rewrites its config at
startup. ``settings.json`` is never touched; user-authored hooks live there.
Every modified file is backed up before it is changed.

The guard fails open. A hook that breaks session start is worse than the stale
config it was meant to remove, so every failure path exits 0 and silently.

Input (stdin): JSON ``{sessionId, cwd, timestamp, reason}`` — unused.
Output: none in hook mode. ``sessionEnd`` hooks ignore stdout.
"""

from __future__ import annotations

import json
import os
import re
import socket
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# A daemon that is still binding its port must not be mistaken for a dead one.
# Configs written within this window are left alone regardless of port state.
FRESH_CONFIG_GRACE = 15 * 60  # seconds

PORT_PROBE_TIMEOUT = 0.35  # seconds

SCRIPT_SUFFIXES = {".cjs", ".js", ".mjs", ".py", ".ps1", ".sh", ".bat", ".cmd"}

# Keys inside a hook entry that hold an executable command line.
COMMAND_KEYS = ("bash", "powershell", "command")

LOOPBACK_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})\b")

# An absolute path, quoted or bare, up to the first quote or whitespace run that
# cannot be part of a path. Windows paths routinely contain spaces, so quoted
# forms are matched first and bare forms are only accepted without spaces.
QUOTED_PATH_RE = re.compile(r"""['"]([A-Za-z]:[\\/][^'"]+|/[^'"]+)['"]""")
BARE_PATH_RE = re.compile(r"(?<!['\"])([A-Za-z]:[\\/][^\s'\"]+|(?<![\w:])/[^\s'\"]+)")


def copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".copilot"


def log(home: Path, message: str) -> None:
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = home / "hook-guard.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def port_is_listening(port: int) -> bool:
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                         (socket.AF_INET6, ("::1", port))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(PORT_PROBE_TIMEOUT)
                if sock.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue
    return False


def commands_of(entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return []
    return [entry[k] for k in COMMAND_KEYS if isinstance(entry.get(k), str)]


def missing_script(command: str) -> str | None:
    """Return an absolute script path referenced by ``command`` that is gone.

    Only absolute paths carrying a known script suffix count. A relative path is
    resolved against a working directory this guard does not know, and a bare
    executable name is resolved against PATH, so neither can be judged here.
    """
    candidates = QUOTED_PATH_RE.findall(command) + BARE_PATH_RE.findall(command)
    for raw in candidates:
        candidate = raw.strip().rstrip("'\";,)")
        if Path(candidate).suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        try:
            if not Path(candidate).exists():
                return candidate
        except OSError:
            continue
    return None


def dead_port(command: str, probe_ports: bool, cache: dict[int, bool]) -> int | None:
    if not probe_ports:
        return None
    for match in LOOPBACK_RE.finditer(command):
        try:
            port = int(match.group(1))
        except ValueError:
            continue
        if not 0 < port < 65536:
            continue
        if port not in cache:
            cache[port] = port_is_listening(port)
        if not cache[port]:
            return port
    return None


def orphan_reason(entry: object, probe_ports: bool, cache: dict[int, bool]) -> str | None:
    for command in commands_of(entry):
        gone = missing_script(command)
        if gone:
            return f"script missing: {gone}"
        port = dead_port(command, probe_ports, cache)
        if port is not None:
            return f"no listener on port {port}"
    return None


def backup(home: Path, path: Path) -> None:
    target_dir = home / "memory-backups" / "hook-guard"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, target_dir / f"{path.stem}-{stamp}{path.suffix}")


def prune_file(home: Path, path: Path, dry_run: bool = False) -> list[str]:
    """Strip orphaned entries from one hook config. Returns removal reasons."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # A config this guard cannot parse is a config it cannot reason about.
        log(home, f"SKIP {path.name} — unparseable JSON")
        return []
    if not isinstance(config, dict):
        return []
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return []

    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        age = FRESH_CONFIG_GRACE + 1
    probe_ports = age > FRESH_CONFIG_GRACE

    cache: dict[int, bool] = {}
    removed: list[str] = []

    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            reason = orphan_reason(entry, probe_ports, cache)
            if reason is None:
                kept.append(entry)
            else:
                removed.append(f"{path.name}:{event} — {reason}")
        if kept:
            hooks[event] = kept
        else:
            # An event whose only handler is gone must not be left declared:
            # an empty registration is what the CLI then fails to dispatch.
            del hooks[event]

    if not removed:
        return []

    if dry_run:
        return removed

    backup(home, path)
    if hooks:
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        log(home, f"PRUNED {path.name} — {len(removed)} entry(ies) removed")
    else:
        path.unlink()
        log(home, f"REMOVED {path.name} — no live hooks remained")
    for reason in removed:
        log(home, f"  {reason}")
    return removed


def main() -> int:
    dry_run = any(a in ("--check", "--dry-run", "-n") for a in sys.argv[1:])

    if not dry_run:
        # Drain stdin so the CLI never blocks writing the hook payload.
        try:
            sys.stdin.read()
        except Exception:
            pass

    home = copilot_home()
    hooks_dir = home / "hooks"
    if not hooks_dir.is_dir():
        if dry_run:
            print(f"no hooks directory at {hooks_dir}")
        return 0

    findings: list[str] = []
    scanned = 0
    for path in sorted(hooks_dir.glob("*.json")):
        scanned += 1
        try:
            findings.extend(prune_file(home, path, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - guard must never break the session
            log(home, f"ERROR {path.name} — {type(exc).__name__}: {exc}")
            if dry_run:
                print(f"ERROR {path.name} — {type(exc).__name__}: {exc}")

    if dry_run:
        print(f"scanned {scanned} hook config(s) in {hooks_dir}")
        if findings:
            print(f"{len(findings)} orphaned entry(ies) — run without --check to prune:")
            for reason in findings:
                print(f"  {reason}")
        else:
            print("no orphaned hook entries found")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: a broken guard must not break the session.
        sys.exit(0)
