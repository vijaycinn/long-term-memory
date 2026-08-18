"""Path and mode resolution for the wiki layer.

The wiki has to run in three quite different situations, and the difference
between them is entirely a matter of what exists on disk:

  1. **Greenfield machine** — memory.db may not even exist yet. Nothing should
     crash; the tools report what is missing and exit cleanly.
  2. **Memory-only** — memory.db exists, no source workspace. A perfectly
     useful wiki is still produced from topics, facts, entities, snapshots and
     the session store.
  3. **Full** — a source workspace is configured, so workspace documents are
     classified and imported alongside the memory-derived pages.

Resolution order for every path is: explicit argument → environment variable →
`~/.copilot/wiki-config.json` → default. Nothing is hardcoded to one machine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

COPILOT_HOME = Path(os.environ.get("COPILOT_HOME") or (Path.home() / ".copilot"))
CONFIG_FILE = COPILOT_HOME / "wiki-config.json"

DEFAULTS = {
    # Source workspace to compile documents from. Empty = memory-only wiki.
    "workspace": "",
    # Where the Obsidian vault is written.
    "vault": str(Path.home() / "ltm-vault"),
    "memory_db": str(COPILOT_HOME / "memory.db"),
    "session_db": str(COPILOT_HOME / "session-store.db"),
}

ENV = {
    "workspace": "LTM_WIKI_WORKSPACE",
    "vault": "LTM_WIKI_VAULT",
    "memory_db": "LTM_MEMORY_DB",
    "session_db": "LTM_SESSION_DB",
}


def _file_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get(key: str, override: str | None = None) -> str:
    if override:
        return str(override)
    env = os.environ.get(ENV.get(key, ""))
    if env:
        return env
    cfg = _file_config()
    if cfg.get(key):
        return str(cfg[key])
    return DEFAULTS[key]


def paths(**overrides) -> dict:
    """Resolved paths plus the capability flags derived from them."""
    ws = get("workspace", overrides.get("workspace"))
    out = {
        "workspace": Path(ws) if ws else None,
        "vault": Path(get("vault", overrides.get("vault"))),
        "memory_db": Path(get("memory_db", overrides.get("memory_db"))),
        "session_db": Path(get("session_db", overrides.get("session_db"))),
        "config_file": CONFIG_FILE,
    }
    out["has_workspace"] = bool(out["workspace"] and out["workspace"].is_dir())
    out["has_memory"] = out["memory_db"].is_file()
    out["has_sessions"] = out["session_db"].is_file()
    out["mode"] = ("full" if out["has_workspace"]
                   else "memory-only" if out["has_memory"]
                   else "empty")
    return out


def save(**values) -> Path:
    """Persist settings to ~/.copilot/wiki-config.json, merging with existing."""
    cfg = _file_config()
    for k, v in values.items():
        if k in DEFAULTS and v is not None:
            cfg[k] = str(v)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return CONFIG_FILE


def describe() -> str:
    p = paths()
    lines = [
        f"mode          : {p['mode']}",
        f"workspace     : {p['workspace'] or '(none - memory-only wiki)'}"
        + ("" if p["has_workspace"] or not p["workspace"] else "   [MISSING]"),
        f"vault         : {p['vault']}" + ("" if p["vault"].is_dir() else "   [not created yet]"),
        f"memory.db     : {p['memory_db']}" + ("" if p["has_memory"] else "   [MISSING]"),
        f"session-store : {p['session_db']}" + ("" if p["has_sessions"] else "   [absent - optional]"),
        f"config file   : {p['config_file']}"
        + ("" if p["config_file"].is_file() else "   [not written - using defaults/env]"),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
