"""H2: 再起動復元（rehydrate_mandate）— プロセスを跨いで認可保証が生き続けること。

決め手はストレージ層（file-backed SQLite の台帳＋監査チェーン）から全量が再導出される
こと：予算・リプレイ（intent/nonce）・単調時刻・親子集計が、エンジンを作り直しても同一
判定になる。in-process な状態に依存していれば、ここのテストが落ちる。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    MandateError,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    TransactionBuilder,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
USDC = Currency.USDC


def _boot(tmp_path, *, fresh: bool):
    """(ledger, audit, engine) を file-backed ストレージで構築する。"""
    ledger = Ledger(SQLiteLedgerStorage(str(tmp_path / "ledger.db")))
    audit = AuditLog(str(tmp_path / "audit.db"))
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    return ledger, audit, eng


def _fund_escrow(ledger, cents: int):
    plat = ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T0)
    b.transfer(plat.account_id, escrow.account_id, Money(cents, USDC))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T0)
    return escrow


class TestRestartSurvival:
    def test_budget_replay_and_monotonic_time_survive_restart(self, tmp_path):
        # --- 初回プロセス：mandate 作成 + 1件決済 ---------------------------------
        ledger, _audit, eng = _boot(tmp_path, fresh=True)
        escrow = _fund_escrow(ledger, 25000)
        payee = ledger.open_account(OwnerType.USER, USDC, "merchant")
        mandate = eng.create_mandate(
            mandate_id="m1", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=Money(25000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=30), created_at=T0,
        )
        r = eng.settle_intent(mandate_id="m1", intent_id="i1",
                              payee_account_id=payee.account_id,
                              amount=Money(10000, USDC), purpose="API_CALL",
                              at=T0 + timedelta(minutes=1))
        assert r.decision == "SETTLED"

        # --- 「再起動」：同じファイルから全コンポーネントを作り直す -----------------
        ledger2, _audit2, eng2 = _boot(tmp_path, fresh=False)
        eng2.rehydrate_mandate(mandate)

        # 予算が引き継がれている（10000 使用済み → 残 15000）
        assert eng2.remaining_cents("m1", as_of=T0 + timedelta(minutes=2)) == 15000

        # リプレイは再起動を跨いでも拒否される（ストレージ層が最後の防衛線）
        ok, reason, _ = eng2.preauthorize(
            mandate_id="m1", intent_id="i1", payee_account_id=payee.account_id,
            amount=Money(10000, USDC), purpose="API_CALL", at=T0 + timedelta(minutes=2))
        assert (ok, reason) == (False, "DUPLICATE_INTENT")

        # 単調時刻も引き継がれる（過去時刻の決済は拒否）
        ok, reason, _ = eng2.preauthorize(
            mandate_id="m1", intent_id="i2", payee_account_id=payee.account_id,
            amount=Money(1000, USDC), purpose="API_CALL", at=T0 + timedelta(seconds=30))
        assert (ok, reason) == (False, "NON_MONOTONIC_TIME")

        # 予算超過も引き継がれる（残 15000 に 20000 は載らない）
        ok, reason, _ = eng2.preauthorize(
            mandate_id="m1", intent_id="i3", payee_account_id=payee.account_id,
            amount=Money(20000, USDC), purpose="API_CALL", at=T0 + timedelta(minutes=3))
        assert (ok, reason) == (False, "BUDGET_EXCEEDED")

        # 正当な決済は通り、以後も同一保証で続く
        r2 = eng2.settle_intent(mandate_id="m1", intent_id="i4",
                                payee_account_id=payee.account_id,
                                amount=Money(15000, USDC), purpose="API_CALL",
                                at=T0 + timedelta(minutes=4))
        assert r2.decision == "SETTLED"
        assert eng2.remaining_cents("m1", as_of=T0 + timedelta(minutes=5)) == 0

    def test_lifecycle_survives_restart(self, tmp_path):
        ledger, _a, eng = _boot(tmp_path, fresh=True)
        escrow = _fund_escrow(ledger, 10000)
        m = eng.create_mandate(
            mandate_id="m1", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=Money(10000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=30), created_at=T0,
        )
        eng.pause_mandate("m1", at=T0 + timedelta(minutes=1), reason="ops")

        _l2, _a2, eng2 = _boot(tmp_path, fresh=False)
        eng2.rehydrate_mandate(m)
        # 一時停止は監査チェーンから再導出される
        assert eng2.mandate_state("m1", at=T0 + timedelta(minutes=2)).state.value == "PAUSED"
        payee = eng2.ledger.open_account(OwnerType.USER, USDC, "merchant2")
        ok, reason, _ = eng2.preauthorize(
            mandate_id="m1", intent_id="i1", payee_account_id=payee.account_id,
            amount=Money(1000, USDC), purpose="API_CALL", at=T0 + timedelta(minutes=2))
        assert (ok, reason) == (False, "MANDATE_PAUSED")


class TestRehydrateGuards:
    def test_double_attach_rejected(self, tmp_path):
        ledger, _a, eng = _boot(tmp_path, fresh=True)
        escrow = _fund_escrow(ledger, 10000)
        m = eng.create_mandate(
            mandate_id="m1", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=Money(10000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=30), created_at=T0,
        )
        with pytest.raises(MandateError, match="already attached"):
            eng.rehydrate_mandate(m)

    def test_wrong_ledger_rejected(self, tmp_path):
        ledger, _a, eng = _boot(tmp_path, fresh=True)
        escrow = _fund_escrow(ledger, 10000)
        m = eng.create_mandate(
            mandate_id="m1", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=Money(10000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=30), created_at=T0,
        )
        other = IntentSettlementEngine(Ledger(SQLiteLedgerStorage(":memory:")), audit_log=None)
        with pytest.raises(MandateError, match="escrow account not found"):
            other.rehydrate_mandate(m)

    def test_child_requires_parent_first(self, tmp_path):
        ledger, _a, eng = _boot(tmp_path, fresh=True)
        escrow = _fund_escrow(ledger, 20000)
        parent = eng.create_mandate(
            mandate_id="root", principal_id="p", escrow_account_id=escrow.account_id,
            budget_cap=Money(20000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=30), created_at=T0,
        )
        child = eng.create_sub_mandate(
            parent_mandate_id="root", mandate_id="child", delegate_id="d",
            sub_budget_cap=Money(5000, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=T0, valid_until=T0 + timedelta(days=1), created_at=T0,
        )
        eng2 = IntentSettlementEngine(ledger, audit_log=None)
        with pytest.raises(MandateError, match="parent mandate must be rehydrated first"):
            eng2.rehydrate_mandate(child)
        eng2.rehydrate_mandate(parent)
        eng2.rehydrate_mandate(child)  # 正しい順序なら装着できる
        assert set(eng2.mandates) == {"root", "child"}
