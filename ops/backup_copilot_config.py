#!/usr/bin/env python3
"""Snapshot Copilot CLI configuration into this repo, with secrets redacted.

Why a script and not a one-off copy: `mcp-config.json` carries a live WebIQ API
key and a GitHub `Authorization` header, so the file cannot be committed as-is.
Redacting by hand is the kind of step that gets skipped the third time. This
makes the safe version the easy version, and re-runnable whenever the config
changes.

`settings.json` and `config.json` are rewritten by the CLI itself, so these
copies are snapshots for reference and disaster recovery, not a live sync.
Restoring is a deliberate act — copy the file back and restart the CLI.

Redaction is deny-by-default on the *key* name, not the value: any key whose
name looks credential-bearing has its value replaced, at any depth, including
inside `headers` and `env` maps. A value that merely looks like a token under
an innocuous key is not caught, so review the diff before committing.

Usage::

    python backup_copilot_config.py            # write snapshots
    python backup_copilot_config.py --check    # report what would change
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REDACTED = "<REDACTED>"

# Matched against key names, case-insensitively, at any nesting depth.
SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|token|secret|password|passwd|authorization|"
    r"credential|client[_-]?secret|access[_-]?key|private[_-]?key|bearer)"
)

# Files copied verbatim after redaction. Anything not listed is not backed up.
CONFIG_FILES = (
    "mcp-config.json",
    "settings.json",
    "permissions-config.json",
)

# Hook configs live in their own directory and are copied wholesale.
HOOKS_SUBDIR = "hooks"


def copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".copilot"


def redact(value):
    """Recursively replace credential-bearing values.

    Keys are judged, not values: a key named `x-apikey` is redacted whatever it
    holds, and a key named `url` is kept even when its value looks random.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)) and not isinstance(item, (dict, list)):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def strip_json_comments(text: str) -> str:
    """Drop leading `//` comment lines.

    `config.json` opens with a banner comment, which is legal for the CLI but
    not for `json.loads`.
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    return "\n".join(lines)


def snapshot(src: Path, dst: Path, dry_run: bool) -> str | None:
    if not src.is_file():
        return None
    try:
        raw = src.read_text(encoding="utf-8")
        data = json.loads(strip_json_comments(raw))
    except (OSError, ValueError) as exc:
        return f"SKIP {src.name} — {type(exc).__name__}: {exc}"

    rendered = json.dumps(redact(data), indent=2) + "\n"

    previous = dst.read_text(encoding="utf-8") if dst.is_file() else None
    if previous == rendered:
        return f"unchanged {dst.name}"
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(rendered, encoding="utf-8")
    verb = "would write" if dry_run else "wrote"
    return f"{verb} {dst.name}"


def main() -> int:
    dry_run = any(a in ("--check", "--dry-run", "-n") for a in sys.argv[1:])
    home = copilot_home()
    out = Path(__file__).resolve().parent.parent / "copilot-config"

    results = []
    for name in CONFIG_FILES:
        result = snapshot(home / name, out / name, dry_run)
        if result:
            results.append(result)

    hooks_src = home / HOOKS_SUBDIR
    if hooks_src.is_dir():
        for path in sorted(hooks_src.glob("*.json")):
            result = snapshot(path, out / HOOKS_SUBDIR / path.name, dry_run)
            if result:
                results.append(result)

    if not dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        (out / "SNAPSHOT.txt").write_text(
            f"Snapshot of {home}\nTaken {stamp}\n"
            f"Secrets redacted by backup_copilot_config.py — review before committing.\n",
            encoding="utf-8",
        )

    for line in results:
        print(line)
    print(f"{len(results)} file(s) processed -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
