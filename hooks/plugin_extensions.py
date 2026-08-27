#!/usr/bin/env python3
"""Disable plugin-contributed Copilot extensions by renaming their entry point.

Why renaming and not configuration
----------------------------------
``settings.json`` has the right key — ``extensions.disabledExtensions``, read via
``userSettingsLoad`` in the CLI bundle — but the extension **ID format** is
undocumented. Five candidates were tested on 2026-08-27 and every one was
ignored::

    plugin-msx-mcp_earnings-dashboard    earnings-dashboard
    msx-mcp/hok-dashboard                msx-mcp_earnings-dashboard
    plugin:msx-mcp/earnings-dashboard

``copilot plugins list`` enumerates plugins, MCP servers, skills, instructions
and language servers — but not extensions — so there is no way to ask the CLI
what the IDs are.

What does work is renaming ``extension.mjs`` to ``extension.disabled.mjs``: the
CLI finds no entry point and never forks the process. This is the same mechanism
that stopped the user-owned ``sequential-thinking`` and ``tts-voice``
extensions, which have not launched since.

The catch is that a plugin update restores the original file, silently
re-enabling the extension. So this runs from ``sessionEnd`` and re-applies the
desired state each time, rather than being a one-shot rename that quietly decays.

Nothing is ever deleted — only renamed — so re-enabling is exact and lossless.

Desired state lives in ``disabled-plugin-extensions.json`` beside this file::

    {"disabled": [{"plugin": "msx-mcp", "extension": "earnings-dashboard"}]}

Usage::

    python plugin_extensions.py --check      # report, change nothing
    python plugin_extensions.py --apply      # enforce the desired state
    python plugin_extensions.py --enable msx-mcp/earnings-dashboard
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ENTRY = "extension.mjs"
DISABLED_ENTRY = "extension.disabled.mjs"
STATE_FILE = Path(__file__).resolve().parent / "disabled-plugin-extensions.json"


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


def load_desired() -> list[dict]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("disabled")
    return entries if isinstance(entries, list) else []


def find_extension_dir(home: Path, plugin: str, extension: str) -> Path | None:
    """Locate an extension directory under any marketplace.

    Installed plugins are nested `installed-plugins/<marketplace>/<plugin>/`, and
    the marketplace an extension arrived from is not something the caller should
    have to know or keep in sync.
    """
    root = home / "installed-plugins"
    if not root.is_dir():
        return None
    for candidate in root.glob(f"*/{plugin}/.github/extensions/{extension}"):
        if candidate.is_dir():
            return candidate
    return None


def apply(home: Path, dry_run: bool) -> list[str]:
    actions: list[str] = []
    for entry in load_desired():
        plugin = entry.get("plugin")
        extension = entry.get("extension")
        if not plugin or not extension:
            continue
        directory = find_extension_dir(home, plugin, extension)
        if directory is None:
            actions.append(f"absent {plugin}/{extension} — plugin not installed")
            continue
        live = directory / ENTRY
        parked = directory / DISABLED_ENTRY
        if not live.is_file():
            state = "already disabled" if parked.is_file() else "no entry point"
            actions.append(f"ok {plugin}/{extension} — {state}")
            continue
        if dry_run:
            actions.append(f"would disable {plugin}/{extension}")
            continue
        try:
            if parked.is_file():
                parked.unlink()
            live.rename(parked)
            actions.append(f"DISABLED {plugin}/{extension}")
            log(home, f"DISABLED extension {plugin}/{extension}")
        except OSError as exc:
            actions.append(f"FAILED {plugin}/{extension} — {exc}")
    return actions


def enable(home: Path, target: str) -> list[str]:
    if "/" not in target:
        return [f"expected <plugin>/<extension>, got {target!r}"]
    plugin, extension = target.split("/", 1)
    directory = find_extension_dir(home, plugin, extension)
    if directory is None:
        return [f"absent {target} — plugin not installed"]
    parked = directory / DISABLED_ENTRY
    live = directory / ENTRY
    if not parked.is_file():
        return [f"ok {target} — already enabled"]
    try:
        parked.rename(live)
    except OSError as exc:
        return [f"FAILED {target} — {exc}"]
    log(home, f"ENABLED extension {target}")
    return [
        f"ENABLED {target}",
        "Remove it from disabled-plugin-extensions.json or sessionEnd will disable it again.",
    ]


def main() -> int:
    args = sys.argv[1:]
    home = copilot_home()

    if "--enable" in args:
        index = args.index("--enable")
        if index + 1 >= len(args):
            print("--enable requires <plugin>/<extension>")
            return 0
        for line in enable(home, args[index + 1]):
            print(line)
        return 0

    dry_run = any(a in ("--check", "--dry-run", "-n") for a in args)
    interactive = dry_run or "--apply" in args

    if not interactive:
        try:
            sys.stdin.read()
        except Exception:
            pass

    actions = apply(home, dry_run)
    if interactive:
        for line in actions:
            print(line)
        if not actions:
            print("nothing configured in disabled-plugin-extensions.json")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: never break the session over a cosmetic optimisation.
        sys.exit(0)
