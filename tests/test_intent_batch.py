"""tests/test_intent_batch.py — アトミックなバッチ決済と構造リーダーの fail-closed 照合。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType, TransactionStatus
from mandatehub.intent import IntentRequest, IntentSettlementEngine, SettlementRecord
from mandatehub.intent.errors import SettlementIntegrityError
from mandatehub.intent.settlement import iter_settlement_records
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _engine(budget=1000, cap=100):
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(budget))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
    pb = ledger.open_account(OwnerType.USER, Currency.USDC, "pb")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(mandate_id="m", principal_id="p", escrow_account_id=escrow.account_id, budget_cap=usdc(cap), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
    return ledger, escrow, pa, pb, eng


def _tx_count(ledger):
    return sum(1 for _ in ledger.iter_all_transactions())


class TestBatch:
    def test_all_pass_one_tx_with_n_escrow_legs(self):
        ledger, escrow, pa, pb, eng = _engine()
        before = _tx_count(ledger)
        r = eng.settle_batch(mandate_id="m", intents=[IntentRequest("i1", pa.account_id, usdc(30), "A"), IntentRequest("i2", pb.account_id, usdc(40), "A")], at=T)
        assert r.decision == "SETTLED"
        assert _tx_count(ledger) == before + 1  # exactly one tx
        assert eng.settled_total_cents("m", T) == usdc(70).cents
        assert eng.payee_receipts_cents("m", T) == {pa.account_id: usdc(30).cents, pb.account_id: usdc(40).cents}

    def test_one_failing_leg_denies_whole_batch_no_write(self):
        ledger, escrow, pa, pb, eng = _engine(cap=100)
        before = _tx_count(ledger)
        r = eng.settle_batch(mandate_id="m", intents=[IntentRequest("i1", pa.account_id, usdc(30), "A"), IntentRequest("i2", pb.account_id, usdc(999), "A")], at=T)
        assert r.decision == "DENIED"
        assert r.reason == "BUDGET_EXCEEDED@i2"
        assert _tx_count(ledger) == before  # nothing written

    def test_within_batch_cumulative_budget(self):
        # 2 レグ個別には OK だが合算で枠超過 → 全体 DENIED
        ledger, escrow, pa, pb, eng = _engine(cap=100)
        r = eng.settle_batch(mandate_id="m", intents=[IntentRequest("i1", pa.account_id, usdc(60), "A"), IntentRequest("i2", pb.account_id, usdc(60), "A")], at=T)
        assert r.decision == "DENIED" and r.reason == "BUDGET_EXCEEDED@i2"

    def test_batch_equals_sequence(self):
        ledger, escrow, pa, pb, eng = _engine()
        eng.settle_batch(mandate_id="m", intents=[IntentRequest("i1", pa.account_id, usdc(30), "A"), IntentRequest("i2", pb.account_id, usdc(20), "A")], at=T)
        assert eng.remaining_cents("m", T) == usdc(50).cents


class TestReaderReconciliation:
    def test_fail_closed_on_batch_multiset_mismatch(self):
        # batch JSON の金額が構造エントリと食い違う tx を手で作る → SettlementIntegrityError
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
        pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
        f = TransactionBuilder("FUND", "ops", initiated_at=T)
        f.transfer(plat.account_id, escrow.account_id, usdc(100))
        ledger.post(f.build())
        ledger.settle(f.transaction_id, settled_at=T)

        b = TransactionBuilder("INTENT_BATCH", "p", initiated_at=T)
        b.add_entry(escrow.account_id, Money(cents=-usdc(30).cents, currency=Currency.USDC))
        b.add_entry(pa.account_id, Money(cents=usdc(30).cents, currency=Currency.USDC))
        b.with_metadata("transaction_type", "INTENT_SETTLEMENT")
        b.with_metadata("mandate_id", "m")
        b.with_metadata("escrow_account_id", escrow.account_id)
        # legs claims amount 50 but structural debit is 30 -> mismatch
        legs = [{"intent_id": "i1", "purpose": "A", "payee_account_id": pa.account_id, "amount_cents": usdc(50).cents, "payee_receipt_cents": usdc(50).cents, "nonce": None, "epoch_index": None}]
        b.with_metadata("batch", json.dumps(legs, sort_keys=True, separators=(",", ":")))
        tx = b.build(status=TransactionStatus.SETTLED, settled_at=T)
        ledger.post(tx)

        with pytest.raises(SettlementIntegrityError):
            list(iter_settlement_records(ledger, as_of=T))
