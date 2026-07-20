"""tests/test_intent_policy.py — EpochSpec / SpendPolicy と DENIAL_ORDER の網羅。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.intent import EpochSpec, IntentSettlementEngine, MandateError, SpendPolicy
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _engine(budget=1000):
    storage = SQLiteLedgerStorage(":memory:")
    ledger = Ledger(storage)
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
    return eng, escrow, pa, pb


class TestEpochSpec:
    def test_epoch_index_boundaries(self):
        e = EpochSpec(anchor=T, length_seconds=3600)
        assert e.epoch_index(T) == 0
        assert e.epoch_index(T + timedelta(seconds=3599)) == 0
        assert e.epoch_index(T + timedelta(microseconds=3600 * 1_000_000 - 1)) == 0
        assert e.epoch_index(T + timedelta(seconds=3600)) == 1
        assert e.epoch_index(T - timedelta(seconds=1)) == -1

    def test_epoch_index_is_int_no_float(self):
        e = EpochSpec(anchor=T, length_seconds=1)
        assert isinstance(e.epoch_index(T + timedelta(microseconds=500_000)), int)

    def test_bad_length_rejected(self):
        with pytest.raises(MandateError):
            EpochSpec(anchor=T, length_seconds=0)


class TestSpendPolicyValidation:
    def test_min_gt_max_rejected(self):
        with pytest.raises(MandateError):
            SpendPolicy(min_amount_cents=100, max_amount_cents=50)

    def test_duplicate_sub_budget_purpose(self):
        with pytest.raises(MandateError):
            SpendPolicy(purpose_sub_budgets=(("A", 10), ("A", 20)))

    def test_epoch_cap_requires_epoch(self):
        with pytest.raises(MandateError):
            SpendPolicy(epoch_spend_cap_cents=100)

    def test_window_cap_requires_window(self):
        with pytest.raises(MandateError):
            SpendPolicy(rolling_window_spend_cap_cents=100)

    def test_sub_budgets_normalized_sorted(self):
        p = SpendPolicy(purpose_sub_budgets=(("B", 10), ("A", 20)))
        assert p.purpose_sub_budgets == (("A", 20), ("B", 10))


class TestDenials:
    def _m(self, eng, escrow, policy=None, per_tx=None, nonce_required=False):
        eng.create_mandate(
            mandate_id="m", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=usdc(1000), allowed_purposes=frozenset(["A", "B"]),
            valid_from=T, valid_until=END, created_at=T,
            per_transaction_limit=per_tx, spend_policy=policy, nonce_required=nonce_required,
        )

    def test_payee_not_allowed(self):
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow, policy=SpendPolicy(payee_allowlist=frozenset([pa.account_id])))
        r = eng.settle_intent(mandate_id="m", intent_id="i", payee_account_id=pb.account_id, amount=usdc(10), purpose="A", at=T)
        assert r.reason == "PAYEE_NOT_ALLOWED"

    def test_min_max_amount(self):
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow, policy=SpendPolicy(min_amount_cents=usdc(5).cents, max_amount_cents=usdc(50).cents))
        assert eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(2), purpose="A", at=T).reason == "BELOW_MIN_AMOUNT"
        assert eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(80), purpose="A", at=T).reason == "ABOVE_MAX_AMOUNT"

    def test_sub_budget(self):
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow, policy=SpendPolicy(purpose_sub_budgets=(("A", usdc(30).cents),)))
        eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(20), purpose="A", at=T)
        r = eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(15), purpose="A", at=T)
        assert r.reason == "SUB_BUDGET_EXCEEDED"
        # 別 purpose は影響を受けない
        assert eng.settle_intent(mandate_id="m", intent_id="i3", payee_account_id=pa.account_id, amount=usdc(100), purpose="B", at=T).decision == "SETTLED"

    def test_epoch_and_window_caps_boundary(self):
        eng, escrow, pa, pb = _engine()
        pol = SpendPolicy(epoch=EpochSpec(anchor=T, length_seconds=3600), epoch_spend_cap_cents=usdc(30).cents)
        self._m(eng, escrow, policy=pol)
        assert eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(30), purpose="A", at=T).decision == "SETTLED"  # exactly cap
        assert eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T + timedelta(minutes=1)).reason == "EPOCH_CAP_EXCEEDED"
        # 次 epoch でリセット
        assert eng.settle_intent(mandate_id="m", intent_id="i3", payee_account_id=pa.account_id, amount=usdc(30), purpose="A", at=T + timedelta(hours=1)).decision == "SETTLED"

    def test_velocity_caps(self):
        eng, escrow, pa, pb = _engine()
        pol = SpendPolicy(epoch=EpochSpec(anchor=T, length_seconds=3600), epoch_settlement_cap=2)
        self._m(eng, escrow, policy=pol)
        eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T)
        eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T + timedelta(minutes=1))
        assert eng.settle_intent(mandate_id="m", intent_id="i3", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T + timedelta(minutes=2)).reason == "EPOCH_VELOCITY_EXCEEDED"

    def test_window_caps(self):
        eng, escrow, pa, pb = _engine()
        pol = SpendPolicy(rolling_window_seconds=3600, rolling_window_spend_cap_cents=usdc(20).cents, rolling_window_settlement_cap=5)
        self._m(eng, escrow, policy=pol)
        eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(15), purpose="A", at=T)
        assert eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(10), purpose="A", at=T + timedelta(minutes=30)).reason == "WINDOW_CAP_EXCEEDED"
        # 窓が過ぎればOK
        assert eng.settle_intent(mandate_id="m", intent_id="i3", payee_account_id=pa.account_id, amount=usdc(10), purpose="A", at=T + timedelta(hours=2)).decision == "SETTLED"

    def test_monotonic_time(self):
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow)
        eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(10), purpose="A", at=T + timedelta(hours=1))
        r = eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(10), purpose="A", at=T)  # backdated
        assert r.reason == "NON_MONOTONIC_TIME"

    def test_nonce_replay(self):
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow, nonce_required=True)
        assert eng.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T, nonce=5).decision == "SETTLED"
        assert eng.settle_intent(mandate_id="m", intent_id="i2", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T, nonce=5).reason == "NONCE_REUSED"
        assert eng.settle_intent(mandate_id="m", intent_id="i3", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T, nonce=3).reason == "NONCE_NOT_INCREASING"
        assert eng.settle_intent(mandate_id="m", intent_id="i4", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T, nonce=None).reason == "NONCE_NOT_INCREASING"
        assert eng.settle_intent(mandate_id="m", intent_id="i5", payee_account_id=pa.account_id, amount=usdc(1), purpose="A", at=T, nonce=6).decision == "SETTLED"

    def test_canonical_order_purpose_beats_per_tx(self):
        # 用途違反 かつ 1件上限超過 → 先に評価される PURPOSE_NOT_ALLOWED が返る
        eng, escrow, pa, pb = _engine()
        self._m(eng, escrow, per_tx=usdc(5))
        r = eng.settle_intent(mandate_id="m", intent_id="i", payee_account_id=pa.account_id, amount=usdc(100), purpose="ZZZ", at=T)
        assert r.reason == "PURPOSE_NOT_ALLOWED"
