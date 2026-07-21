"""Offline revenue/usage report from an operator data dir (or a backup snapshot).

Reads the ledger read-only (WAL-safe copy) and prints settlements, revenue, unique payers,
and a per-day breakdown — the same metrics the live /metrics endpoint serves. Point it at a
backup to report on a past state.

    python deploy/local/stats.py                        # ~/.mandatehub-operator
    MANDATEHUB_DATA_DIR=…/backups/<stamp> python deploy/local/stats.py --json
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mandatehub.core.ledger import Ledger
from mandatehub.core.storage import SQLiteLedgerStorage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _metrics import compute_metrics  # noqa: E402


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    data_dir = Path(os.environ.get("MANDATEHUB_DATA_DIR",
                                   str(Path.home() / ".mandatehub-operator")))
    if not (data_dir / "ledger.db").exists():
        print(f"no ledger.db in {data_dir}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="mh-stats-"))
    try:
        s = sqlite3.connect(str(data_dir / "ledger.db"))
        try:
            d = sqlite3.connect(str(tmp / "ledger.db"))
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()

        ledger = Ledger(SQLiteLedgerStorage(str(tmp / "ledger.db")))
        m = compute_metrics(ledger, now=datetime.now(timezone.utc))

        if as_json:
            print(json.dumps(m, indent=2))
            return 0

        print(f"mandatehub operator — usage report ({data_dir})")
        print(f"  settlements:      {m['settlements']}  "
              f"(unique intents {m['unique_intents']}, payees {m['unique_payees']})")
        print(f"  revenue:          {m['revenue_cents'] / 1e6:.6f} USDC "
              f"(budget booked {m['budget_booked_cents'] / 1e6:.6f})")
        print(f"  window:           {m['first_settled_at']} → {m['last_settled_at']}")
        if m["per_day"]:
            print("  per day:")
            for day, d in m["per_day"].items():
                print(f"    {day}: {d['count']:>4} calls   {d['revenue_cents'] / 1e6:.6f} USDC")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
