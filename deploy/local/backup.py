"""Consistent, verified backups of the operator's money-path state.

The ledger and audit databases run in WAL mode, so a plain file copy can be torn or stale.
This uses SQLite's ONLINE BACKUP API (a live, consistent snapshot even under concurrent
writes), then re-opens each snapshot and **verifies the audit hash chain** before keeping it —
a backup that doesn't verify is not counted (fail-closed). `mandate.json` is copied too so a
restore is self-contained.

    python deploy/local/backup.py                 # backup ~/.mandatehub-operator
    MANDATEHUB_DATA_DIR=… MANDATEHUB_BACKUP_DIR=… MANDATEHUB_BACKUP_KEEP=48 python …/backup.py

Restore: stop the operator, copy a snapshot's ledger.db/audit.db/mandate.json back into the
data dir, restart. Because state is re-derived from these files, the operator resumes with the
exact budget/replay/lifecycle it had at snapshot time.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from mandatehub import AuditLog


def _online_backup(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)  # consistent snapshot, WAL-safe
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def main() -> int:
    data_dir = Path(os.environ.get("MANDATEHUB_DATA_DIR",
                                   str(Path.home() / ".mandatehub-operator")))
    backup_root = Path(os.environ.get("MANDATEHUB_BACKUP_DIR", str(data_dir / "backups")))
    keep = int(os.environ.get("MANDATEHUB_BACKUP_KEEP", "48"))

    if not (data_dir / "audit.db").exists():
        print(f"nothing to back up: {data_dir / 'audit.db'} missing", file=sys.stderr)
        return 1
    has_ledger = (data_dir / "ledger.db").exists()  # absent on Postgres-backed operators

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snap = backup_root / stamp
    snap.mkdir(parents=True, exist_ok=True)

    if has_ledger:
        _online_backup(data_dir / "ledger.db", snap / "ledger.db")
    _online_backup(data_dir / "audit.db", snap / "audit.db")
    if (data_dir / "mandate.json").exists():
        shutil.copy2(data_dir / "mandate.json", snap / "mandate.json")

    # Verify the snapshot before trusting it: the audit hash chain must be intact.
    ok, err = AuditLog(str(snap / "audit.db")).verify_chain()
    if not ok:
        shutil.rmtree(snap, ignore_errors=True)
        print(f"BACKUP REJECTED — audit chain invalid on snapshot: {err}", file=sys.stderr)
        return 2

    # Retention: keep the newest `keep` verified snapshots.
    snaps = sorted(d for d in backup_root.iterdir() if d.is_dir())
    for old in snaps[:-keep] if keep > 0 else []:
        shutil.rmtree(old, ignore_errors=True)

    size = sum(f.stat().st_size for f in snap.iterdir())
    note = "" if has_ledger else "; ledger external (Postgres) — back it up via pg_dump"
    print(f"backup OK: {snap}  ({size} bytes, audit chain verified"
          f"{note}; {len(snaps[-keep:]) if keep > 0 else len(snaps)} kept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
