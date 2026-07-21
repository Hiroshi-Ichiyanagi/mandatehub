"""Independently re-verify an operator's money-path state from its data dir (read-only).

The "verify me" tool for a running (or backed-up) operator: open the ledger + audit
databases, check the audit hash chain, rehydrate the mandate, and recompute budget /
collateralization / settlement counts from storage — trusting nothing but the files. Use it
in an incident (suspected tamper), after a restore, or as a periodic self-audit.

    python deploy/local/verify_state.py                 # ~/.mandatehub-operator
    MANDATEHUB_DATA_DIR=/path/to/backups/<stamp> python deploy/local/verify_state.py

Exit 0 = consistent; non-zero = a problem is described on stderr. Opens copies so it never
disturbs a live operator's WAL.
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

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Mandate,
    Money,
    SQLiteLedgerStorage,
)


def _snapshot(src: Path, dst: Path) -> None:
    s = sqlite3.connect(str(src))
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def main() -> int:
    data_dir = Path(os.environ.get("MANDATEHUB_DATA_DIR",
                                   str(Path.home() / ".mandatehub-operator")))
    cfg_path = data_dir / "mandate.json"
    if not cfg_path.exists():
        print(f"no mandate.json in {data_dir}", file=sys.stderr)
        return 1

    # Work on WAL-consistent copies so a live operator is never disturbed.
    tmp = Path(tempfile.mkdtemp(prefix="mh-verify-"))
    try:
        _snapshot(data_dir / "ledger.db", tmp / "ledger.db")
        _snapshot(data_dir / "audit.db", tmp / "audit.db")

        audit = AuditLog(str(tmp / "audit.db"))
        ok, err = audit.verify_chain()
        print(f"audit chain: {'OK' if ok else 'INVALID'}"
              + ("" if ok else f" — {err}"))
        if not ok:
            return 2
        print(f"audit root:  {audit.latest_hash()}")

        cfg = json.loads(cfg_path.read_text())
        ledger = Ledger(SQLiteLedgerStorage(str(tmp / "ledger.db")))
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        mandate = Mandate(
            mandate_id=cfg["mandate_id"], principal_id=cfg["principal_id"],
            escrow_account_id=cfg["escrow_account_id"], currency=Currency.USDC,
            budget_cap=Money(cfg["budget_cap_cents"], Currency.USDC),
            allowed_purposes=frozenset(cfg["allowed_purposes"]),
            valid_from=datetime.fromisoformat(cfg["valid_from"]),
            valid_until=datetime.fromisoformat(cfg["valid_until"]),
            created_at=datetime.fromisoformat(cfg["created_at"]),
        )
        eng.rehydrate_mandate(mandate)

        now = datetime.now(timezone.utc)
        mid = cfg["mandate_id"]
        remaining = eng.remaining_cents(mid, as_of=now)
        settled = eng.settled_total_cents(mid, as_of=now)
        eff_cap = eng.effective_cap_cents(mid, at=now)
        escrow_bal = eng.escrow_balance_cents(mid, as_of=now)
        co_escrow = eng.co_escrow_remaining_cents(mid, as_of=now)

        print(f"mandate:     {mid}  state={eng.mandate_state(mid, at=now).state.value}")
        print(f"budget:      cap={eff_cap}  settled={settled}  remaining={remaining} (cents)")
        print(f"escrow:      balance={escrow_bal}  outstanding_commitments={co_escrow}")

        problems = []
        if remaining < 0:
            problems.append("remaining budget is NEGATIVE (overspend)")
        if escrow_bal < co_escrow:
            problems.append("escrow UNDER-collateralized (balance < outstanding)")
        if settled + remaining != eff_cap:
            problems.append("budget arithmetic mismatch (settled + remaining != cap)")
        if problems:
            for p in problems:
                print(f"PROBLEM: {p}", file=sys.stderr)
            return 3

        print("STATE CONSISTENT: chain intact, within budget, fully collateralized.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
