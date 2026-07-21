"""Ops tools (deploy/local): backup, verify_state, monitor — offline, tamper-aware."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    TransactionBuilder,
)

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "local"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
USDC = Currency.USDC


def _make_operator_state(data_dir: Path) -> None:
    """Build a realistic operator data dir: funded escrow, mandate, one settlement, config."""
    ledger = Ledger(SQLiteLedgerStorage(str(data_dir / "ledger.db")))
    audit = AuditLog(str(data_dir / "audit.db"))
    plat = ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T0)
    b.transfer(plat.account_id, escrow.account_id, Money(1000000, USDC))
    ledger.post(b.build()); ledger.settle(b.transaction_id, settled_at=T0)
    merchant = ledger.open_account(OwnerType.USER, USDC, "merchant")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="operator-m1", principal_id="operator", escrow_account_id=escrow.account_id,
        budget_cap=Money(1000000, USDC), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T0, valid_until=T0 + timedelta(days=365), created_at=T0)
    eng.settle_intent(mandate_id="operator-m1", intent_id="i1",
                      payee_account_id=merchant.account_id, amount=Money(10000, USDC),
                      purpose="API_CALL", at=T0 + timedelta(minutes=1))
    (data_dir / "mandate.json").write_text(json.dumps({
        "mandate_id": "operator-m1", "principal_id": "operator",
        "escrow_account_id": escrow.account_id, "budget_cap_cents": 1000000,
        "allowed_purposes": ["API_CALL"], "valid_from": T0.isoformat(),
        "valid_until": (T0 + timedelta(days=365)).isoformat(), "created_at": T0.isoformat(),
        "merchant_account_id": merchant.account_id}))


def _run(script: str, data_dir: Path, extra_env=None):
    import os
    env = {**os.environ, "MANDATEHUB_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPO)}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(DEPLOY / script)],
                          capture_output=True, text=True, env=env)


def test_backup_creates_verified_snapshot(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    r = _run("backup.py", data, {"MANDATEHUB_BACKUP_DIR": str(tmp_path / "bak")})
    assert r.returncode == 0, r.stderr
    snaps = list((tmp_path / "bak").iterdir())
    assert len(snaps) == 1
    for f in ("ledger.db", "audit.db", "mandate.json"):
        assert (snaps[0] / f).exists()
    ok, _ = AuditLog(str(snaps[0] / "audit.db")).verify_chain()
    assert ok


def test_verify_state_consistent(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    r = _run("verify_state.py", data)
    assert r.returncode == 0, r.stderr
    assert "STATE CONSISTENT" in r.stdout
    assert "audit chain: OK" in r.stdout


def test_verify_state_detects_semantic_tamper(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    conn = sqlite3.connect(str(data / "audit.db"))
    seq, payload = conn.execute(
        "SELECT sequence, payload FROM audit_events WHERE event_type='intent_settled' "
        "ORDER BY sequence LIMIT 1").fetchone()
    d = json.loads(payload); d["amount_cents"] = d["amount_cents"] + 1
    conn.execute("UPDATE audit_events SET payload=? WHERE sequence=?",
                 (json.dumps(d, sort_keys=True), seq))
    conn.commit(); conn.close()
    r = _run("verify_state.py", data)
    assert r.returncode != 0
    assert "INVALID" in r.stdout


def test_verify_state_ignores_whitespace_representation(tmp_path):
    """A JSON-representation change (not a value change) is NOT tampering."""
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    conn = sqlite3.connect(str(data / "audit.db"))
    conn.execute("UPDATE audit_events SET payload = payload || ' ' "
                 "WHERE sequence=(SELECT MIN(sequence) FROM audit_events)")
    conn.commit(); conn.close()
    r = _run("verify_state.py", data)
    assert r.returncode == 0, r.stdout + r.stderr


def test_stats_reports_revenue(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)   # one 10000-cent settlement
    r = _run("stats.py", data, {"MANDATEHUB_ARGS": ""})
    # stats.py takes --json via argv; call directly for determinism
    import os
    import subprocess
    env = {**os.environ, "MANDATEHUB_DATA_DIR": str(data), "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, str(DEPLOY / "stats.py"), "--json"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    m = json.loads(out.stdout)
    assert m["settlements"] == 1
    assert m["revenue_cents"] == 10000
    assert m["unique_payees"] == 1
    assert list(m["per_day"].values())[0]["count"] == 1
