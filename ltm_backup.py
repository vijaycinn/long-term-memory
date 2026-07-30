#!/usr/bin/env python3
"""
ltm_backup.py — Business continuity for the long-term memory store.

Why not just copy the file: memory.db runs in WAL mode, so a plain file copy
can capture a torn database whose committed transactions live in a -wal
sidecar. This module uses SQLite's online backup API, which takes a consistent
snapshot while the database is in use.

Every backup is verified immediately after it is written (PRAGMA
integrity_check plus row-count sanity), so a corrupt backup is detected at
creation rather than during an emergency restore.

Operations:
    backup    consistent snapshot + verify + rotate + optional off-machine copy
    verify    integrity-check an existing backup
    restore   restore a backup, taking a safety copy of the current DB first
    status    inventory, age of newest backup, RPO/RTO posture
    list      enumerate available backups

Usage:
    echo '{"op":"backup"}'  | python ltm_backup.py
    echo '{"op":"status"}'  | python ltm_backup.py
    echo '{"op":"restore","backup":"...","confirm":true}' | python ltm_backup.py
"""

import datetime
import glob
import io
import json
import os
import shutil
import sqlite3
import sys

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".copilot", "memory.db")
DEFAULT_BACKUP_DIR = os.path.join(HOME, ".copilot", "memory-backups")
DEFAULT_KEEP = 14                 # ~2 weeks of daily snapshots
STALE_BACKUP_HOURS = 48           # newest backup older than this = degraded


def _ensure_utf8_stdin():
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "buffer"):
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    except (ValueError, AttributeError):
        pass


def _db_path(p):
    return p.get("db_path") or DEFAULT_DB


def _backup_dir(p):
    return p.get("backup_dir") or DEFAULT_BACKUP_DIR


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_path(dest_dir, stamp):
    """Collision-proof backup path.

    The stamp is second-granular for readability, so two snapshots taken inside
    the same second would otherwise resolve to the same filename and the second
    would silently overwrite the first — losing a recovery point.
    """
    candidate = os.path.join(dest_dir, f"memory-{stamp}.db")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(dest_dir, f"memory-{stamp}-{n}.db")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _counts(conn):
    out = {}
    for table in ("topics", "facts", "entities", "snapshots"):
        try:
            out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = None
    return out


def verify(p):
    """Integrity-check a database file and return its row counts."""
    path = p.get("path") or _db_path(p)
    if not os.path.exists(path):
        return {"ok": False, "path": path, "error": "file not found"}

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = _counts(conn)
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "path": path, "error": f"unreadable: {exc}"}
    finally:
        conn.close()

    ok = integrity == "ok" and not fk and counts.get("facts") is not None
    return {
        "ok": ok,
        "path": path,
        "integrity": integrity,
        "foreign_key_violations": len(fk),
        "counts": counts,
        "size_bytes": os.path.getsize(path),
    }


def backup(p):
    """Take a consistent snapshot, verify it, rotate old copies, optionally mirror."""
    src = _db_path(p)
    dest_dir = _backup_dir(p)
    keep = int(p.get("keep", DEFAULT_KEEP))

    if not os.path.exists(src):
        return {"ok": False, "error": f"source database not found: {src}"}

    os.makedirs(dest_dir, exist_ok=True)
    dest = _unique_path(dest_dir, _stamp())

    # Online backup API — consistent even while the DB is being written.
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    check = verify({"path": dest})
    if not check["ok"]:
        os.remove(dest)
        return {"ok": False, "error": "backup failed verification and was discarded",
                "verify": check}

    rotated = rotate(dest_dir, keep)

    mirror = None
    mirror_dir = p.get("mirror_dir")
    if mirror_dir:
        try:
            os.makedirs(mirror_dir, exist_ok=True)
            mirror_path = os.path.join(mirror_dir, os.path.basename(dest))
            shutil.copy2(dest, mirror_path)
            mirror = {"ok": True, "path": mirror_path}
        except OSError as exc:
            mirror = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "path": dest,
        "size_bytes": check["size_bytes"],
        "counts": check["counts"],
        "rotated": rotated,
        "mirror": mirror,
    }


def rotate(dest_dir, keep):
    """Keep the newest `keep` snapshots, delete the rest."""
    files = sorted(glob.glob(os.path.join(dest_dir, "memory-*.db")), reverse=True)
    removed = []
    for old in files[keep:]:
        try:
            os.remove(old)
            removed.append(os.path.basename(old))
        except OSError:
            pass
    return {"kept": min(len(files), keep), "removed": removed}


def list_backups(p):
    dest_dir = _backup_dir(p)
    files = sorted(glob.glob(os.path.join(dest_dir, "memory-*.db")), reverse=True)
    now = datetime.datetime.now()
    out = []
    for f in files:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f))
        out.append({
            "name": os.path.basename(f),
            "path": f,
            "size_bytes": os.path.getsize(f),
            "age_hours": round((now - mtime).total_seconds() / 3600, 1),
        })
    return {"backup_dir": dest_dir, "count": len(out), "backups": out}


def restore(p):
    """Restore a backup over the live database, after saving the current one.

    Requires {"confirm": true} so it cannot fire accidentally.
    """
    target = _db_path(p)
    chosen = p.get("backup")

    if not chosen:
        available = list_backups(p)["backups"]
        if not available:
            return {"ok": False, "error": "no backups available"}
        chosen = available[0]["path"]

    if not os.path.exists(chosen):
        return {"ok": False, "error": f"backup not found: {chosen}"}

    check = verify({"path": chosen})
    if not check["ok"]:
        return {"ok": False, "error": "refusing to restore: backup failed verification",
                "verify": check}

    if not p.get("confirm"):
        return {"ok": False, "requires_confirm": True,
                "would_restore": chosen, "into": target, "verify": check}

    # Safety net: keep whatever is currently live before overwriting it.
    pre = None
    if os.path.exists(target):
        pre = f"{target}.pre-restore-{_stamp()}"
        shutil.copy2(target, pre)

    # Remove WAL sidecars so the restored file is authoritative.
    for sidecar in (target + "-wal", target + "-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)

    shutil.copy2(chosen, target)
    after = verify({"path": target})
    return {
        "ok": after["ok"],
        "restored_from": chosen,
        "into": target,
        "pre_restore_copy": pre,
        "verify": after,
    }


def status(p):
    """Continuity posture: is there a recent, verified backup?"""
    src = _db_path(p)
    inv = list_backups(p)
    live = verify({"path": src}) if os.path.exists(src) else {"ok": False,
                                                             "error": "live DB missing"}
    newest = inv["backups"][0] if inv["backups"] else None

    if newest is None:
        posture, detail = "AT RISK", "no backups exist"
    elif newest["age_hours"] > STALE_BACKUP_HOURS:
        posture, detail = "DEGRADED", (
            f"newest backup is {newest['age_hours']}h old "
            f"(threshold {STALE_BACKUP_HOURS}h)")
    elif not live.get("ok"):
        posture, detail = "AT RISK", "live database failed integrity check"
    else:
        posture, detail = "HEALTHY", (
            f"newest backup {newest['age_hours']}h old, {inv['count']} retained")

    return {
        "posture": posture,
        "detail": detail,
        "live": live,
        "backup_count": inv["count"],
        "newest_backup": newest,
        "backup_dir": inv["backup_dir"],
        "rpo_hours": newest["age_hours"] if newest else None,
    }


DISPATCH = {
    "backup": backup,
    "verify": verify,
    "restore": restore,
    "status": status,
    "list": list_backups,
}

if __name__ == "__main__":
    _ensure_utf8_stdin()
    try:
        payload = json.loads(sys.stdin.read() or '{"op":"status"}')
        op = payload.get("op", "status")
        if op not in DISPATCH:
            print(json.dumps({"error": f"Unknown op: {op}. Valid: {list(DISPATCH)}"}))
            sys.exit(1)
        print(json.dumps(DISPATCH[op](payload), default=str))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        sys.exit(1)
