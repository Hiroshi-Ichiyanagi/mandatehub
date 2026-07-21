"""PostgresLedgerStorage against a REAL Postgres — engine parity + concurrent replay safety.

Skipped unless a Postgres is reachable (psycopg installed and MANDATEHUB_TEST_PG set, default
`dbname=mandatehub_test host=/tmp`). CI has no Postgres, so these skip there; run locally with
`brew services start postgresql@16 && createdb mandatehub_test`.

The headline test spawns real PROCESSES that all settle the SAME intent through the engine on
one shared database — exactly one may win (the atomic unique-PK claim), proving multi-worker
double-spend safety end-to-end (not just at the SQL layer).
"""
from __future__ import annotations

import multiprocessing as mp
import os
from datetime import datetime, timedelta, timezone

import pytest

CONN = os.environ.get("MANDATEHUB_TEST_PG", "dbname=mandatehub_test host=/tmp")

psycopg = pytest.importorskip("psycopg")


def _pg_available() -> bool:
    try:
        psycopg.connect(CONN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(),
                                reason=f"no reachable Postgres at {CONN!r}")

from mandatehub import (  # noqa: E402
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    ProofOfMandateGenerator,
    TransactionBuilder,
)
from mandatehub.intent.mandate import Mandate  # noqa: E402
from mandatehub.storage_postgres import PostgresLedgerStorage  # noqa: E402

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
USDC = Currency.USDC


def _fresh_storage():
    """A clean PostgresLedgerStorage (drops the three tables first)."""
    s = PostgresLedgerStorage(CONN)
    with s._conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS entries, transactions, accounts, settlement_claims CASCADE")
    s.close()
    return PostgresLedgerStorage(CONN)


def _seed(storage):
    ledger = Ledger(storage)
    plat = ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, Money(1000000, USDC))
    ledger.post(b.build()); ledger.settle(b.transaction_id, settled_at=T)
    payee = ledger.open_account(OwnerType.USER, USDC, "merchant")
    eng = IntentSettlementEngine(ledger, audit_log=AuditLog(":memory:"))
    eng.create_mandate(
        mandate_id="m1", principal_id="p", escrow_account_id=escrow.account_id,
        budget_cap=Money(1000000, USDC), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T)
    return eng, escrow.account_id, payee.account_id


def test_engine_flow_on_postgres():
    eng, _escrow, payee = _seed(_fresh_storage())
    r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=payee,
                          amount=Money(30000, USDC), purpose="API_CALL",
                          at=T + timedelta(minutes=1))
    assert r.decision == "SETTLED"
    assert eng.remaining_cents("m1", as_of=T + timedelta(minutes=2)) == 970000
    # replay denied via the claim
    r2 = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=payee,
                           amount=Money(30000, USDC), purpose="API_CALL",
                           at=T + timedelta(minutes=2))
    assert (r2.decision, r2.reason) == ("DENIED", "DUPLICATE_INTENT")
    proof, _ = ProofOfMandateGenerator(eng).generate("m1", snapshot_at=T + timedelta(minutes=2))
    assert proof.is_within_budget and proof.remaining_cents == 970000


def test_try_claim_is_atomic():
    s = _fresh_storage()
    assert s.try_claim("k", at=T) is True
    assert s.try_claim("k", at=T) is False
    assert s.try_claim("k2", at=T) is True


# --- the multi-worker headline ---------------------------------------------------------

def _race_worker(escrow_id: str, payee_id: str, barrier, q) -> None:
    eng = IntentSettlementEngine(Ledger(PostgresLedgerStorage(CONN)), audit_log=AuditLog(":memory:"))
    eng.rehydrate_mandate(Mandate(
        mandate_id="m1", principal_id="p", escrow_account_id=escrow_id, currency=USDC,
        budget_cap=Money(1000000, USDC), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T))
    barrier.wait()
    r = eng.settle_intent(mandate_id="m1", intent_id="SAME", payee_account_id=payee_id,
                          amount=Money(10000, USDC), purpose="API_CALL",
                          at=T + timedelta(minutes=1))
    q.put(r.decision)


def test_concurrent_processes_cannot_double_settle():
    _eng, escrow_id, payee_id = _seed(_fresh_storage())
    n = 8
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n)
    q = ctx.Queue()
    ps = [ctx.Process(target=_race_worker, args=(escrow_id, payee_id, barrier, q))
          for _ in range(n)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=60)
    results = [q.get() for _ in range(n)]
    assert results.count("SETTLED") == 1, results   # EXACTLY ONE across 8 processes
    assert results.count("DENIED") == n - 1

    # and the ledger holds exactly one settlement of that intent
    s = PostgresLedgerStorage(CONN)
    with s._conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM transactions WHERE metadata LIKE %s", ("%SAME%",))
        assert cur.fetchone()[0] == 1
