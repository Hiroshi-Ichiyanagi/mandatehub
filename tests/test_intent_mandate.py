"""tests/test_intent_mandate.py — 委任枠（Mandate）と自律決済エンジンのテスト。

インテントベースの自律決済の信頼命題「枠を一度も超えていない」を、
成立・却下の各経路について検証する。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType, TransactionStatus
from mandatehub.intent import IntentSettlementEngine, Mandate, MandateError
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = T + timedelta(days=30)


def usdc(units: int) -> Money:
    return Money.from_units(units, Currency.USDC)


@pytest.fixture
def env():
    """元帳 + 監査ログ + 資金化済み escrow(100 USDC) + 受取口座2つ を用意する。"""
    storage = SQLiteLedgerStorage(":memory:")
    ledger = Ledger(storage)
    audit = AuditLog(":memory:")
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "mandate-escrow")
    payee_a = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider-A")
    payee_b = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider-B")

    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(platform.account_id, escrow.account_id, usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)

    engine = IntentSettlementEngine(ledger, audit_log=audit)
    yield {
        "ledger": ledger,
        "audit": audit,
        "engine": engine,
        "escrow": escrow,
        "payee_a": payee_a,
        "payee_b": payee_b,
        "platform": platform,
    }
    storage.close()
    audit.close()


def _mandate(engine: IntentSettlementEngine, escrow_id: str, **overrides) -> Mandate:
    kwargs = dict(
        mandate_id="m1",
        principal_id="agent-orchestrator",
        escrow_account_id=escrow_id,
        budget_cap=usdc(100),
        allowed_purposes=frozenset(["API_CALL", "DATA_STREAM"]),
        valid_from=T,
        valid_until=WINDOW_END,
        created_at=T,
        per_transaction_limit=usdc(40),
    )
    kwargs.update(overrides)
    return engine.create_mandate(**kwargs)


# ---------- 委任枠の成立条件 ----------


class TestMandateCreation:
    def test_fully_collateralized_mandate_is_created(self, env):
        m = _mandate(env["engine"], env["escrow"].account_id)
        assert m.mandate_id == "m1"
        assert m.budget_cap == usdc(100)

    def test_under_collateralized_mandate_rejected(self, env):
        # escrow は 100 USDC だが 150 USDC の枠を張ろうとする → 拒否
        with pytest.raises(MandateError, match="under-collateralized"):
            _mandate(env["engine"], env["escrow"].account_id, budget_cap=usdc(150))

    def test_duplicate_mandate_id_rejected(self, env):
        _mandate(env["engine"], env["escrow"].account_id)
        with pytest.raises(MandateError, match="already exists"):
            _mandate(env["engine"], env["escrow"].account_id)

    def test_budget_currency_mismatch_rejected(self, env):
        with pytest.raises(MandateError):
            Mandate(
                mandate_id="x",
                principal_id="p",
                escrow_account_id="e",
                currency=Currency.USDC,
                budget_cap=Money.from_units(1, Currency.JPY),
                allowed_purposes=frozenset(["A"]),
                valid_from=T,
                valid_until=WINDOW_END,
                created_at=T,
            )

    def test_creation_emits_audit_event(self, env):
        _mandate(env["engine"], env["escrow"].account_id)
        types = [e.event_type for e in env["audit"].iter_events()]
        assert "mandate_created" in types


# ---------- 枠内決済（成立経路） ----------


class TestSettlement:
    def test_settlement_moves_ledger_balances(self, env):
        eng, ledger = env["engine"], env["ledger"]
        _mandate(eng, env["escrow"].account_id)
        r = eng.settle_intent(
            mandate_id="m1",
            intent_id="i1",
            payee_account_id=env["payee_a"].account_id,
            amount=usdc(30),
            purpose="API_CALL",
            at=T,
        )
        assert r.is_settled
        assert r.transaction_id is not None
        assert ledger.balance(env["payee_a"].account_id, as_of=T) == usdc(30)
        # escrow は 100 - 30 = 70 に減る
        assert ledger.balance(env["escrow"].account_id, as_of=T) == usdc(70)

    def test_cumulative_budget_tracked_from_ledger(self, env):
        eng = env["engine"]
        _mandate(eng, env["escrow"].account_id)
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(30), purpose="API_CALL", at=T)
        eng.settle_intent(mandate_id="m1", intent_id="i2", payee_account_id=env["payee_b"].account_id, amount=usdc(25), purpose="DATA_STREAM", at=T)
        assert eng.settled_total_cents("m1", T) == usdc(55).cents
        assert eng.remaining_cents("m1", T) == usdc(45).cents
        assert eng.payee_receipts_cents("m1", T) == {
            env["payee_a"].account_id: usdc(30).cents,
            env["payee_b"].account_id: usdc(25).cents,
        }

    def test_budget_can_be_fully_drained(self, env):
        eng, ledger = env["engine"], env["ledger"]
        _mandate(eng, env["escrow"].account_id)
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(40), purpose="API_CALL", at=T)
        eng.settle_intent(mandate_id="m1", intent_id="i2", payee_account_id=env["payee_a"].account_id, amount=usdc(40), purpose="API_CALL", at=T)
        r = eng.settle_intent(mandate_id="m1", intent_id="i3", payee_account_id=env["payee_a"].account_id, amount=usdc(20), purpose="API_CALL", at=T)
        assert r.is_settled
        assert eng.remaining_cents("m1", T) == 0
        assert ledger.balance(env["escrow"].account_id, as_of=T) == usdc(0)

    def test_settlement_emits_audit_event(self, env):
        eng = env["engine"]
        _mandate(eng, env["escrow"].account_id)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(30), purpose="API_CALL", at=T)
        settled = [e for e in env["audit"].iter_events() if e.event_type == "intent_settled"]
        assert len(settled) == 1
        assert settled[0].payload["intent_id"] == "i1"
        assert settled[0].sequence == r.audit_sequence


# ---------- 枠外決済（却下経路） ----------


class TestDenials:
    def _m(self, env):
        _mandate(env["engine"], env["escrow"].account_id)

    def _tx_count(self, ledger: Ledger) -> int:
        return sum(1 for _ in ledger.iter_all_transactions())

    def test_budget_exceeded_denied(self, env):
        eng, ledger = env["engine"], env["ledger"]
        self._m(env)
        # per-tx 上限(40)以内だが残枠を超える構成にする: 40 + 40 = 80 消化 → 残20、30を要求
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(40), purpose="API_CALL", at=T)
        eng.settle_intent(mandate_id="m1", intent_id="i2", payee_account_id=env["payee_a"].account_id, amount=usdc(40), purpose="API_CALL", at=T)
        before = self._tx_count(ledger)
        r = eng.settle_intent(mandate_id="m1", intent_id="i3", payee_account_id=env["payee_a"].account_id, amount=usdc(30), purpose="API_CALL", at=T)
        assert r.decision == "DENIED"
        assert r.reason == "BUDGET_EXCEEDED"
        # 却下は元帳に一切書かない
        assert self._tx_count(ledger) == before
        assert eng.remaining_cents("m1", T) == usdc(20).cents

    def test_per_tx_limit_denied(self, env):
        eng = env["engine"]
        self._m(env)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(50), purpose="API_CALL", at=T)
        assert r.reason == "PER_TX_LIMIT_EXCEEDED"

    def test_purpose_not_allowed_denied(self, env):
        eng = env["engine"]
        self._m(env)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="GAMBLING", at=T)
        assert r.reason == "PURPOSE_NOT_ALLOWED"

    def test_before_window_denied(self, env):
        eng = env["engine"]
        self._m(env)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="API_CALL", at=T - timedelta(seconds=1))
        assert r.reason == "OUTSIDE_WINDOW"

    def test_after_window_is_expired(self, env):
        # 有効期限超過はライフサイクル状態 EXPIRED として却下される（OUTSIDE_WINDOW より優先）
        eng = env["engine"]
        self._m(env)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="API_CALL", at=WINDOW_END + timedelta(seconds=1))
        assert r.reason == "MANDATE_EXPIRED"

    def test_duplicate_intent_denied(self, env):
        eng = env["engine"]
        self._m(env)
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="API_CALL", at=T)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="API_CALL", at=T)
        assert r.reason == "DUPLICATE_INTENT"
        # 累計は 1 件分のまま
        assert eng.settled_total_cents("m1", T) == usdc(10).cents

    def test_non_positive_amount_denied(self, env):
        eng = env["engine"]
        self._m(env)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=Money(cents=0, currency=Currency.USDC), purpose="API_CALL", at=T)
        assert r.reason == "NON_POSITIVE_AMOUNT"

    def test_denial_emits_audit_event(self, env):
        eng = env["engine"]
        self._m(env)
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(10), purpose="GAMBLING", at=T)
        denied = [e for e in env["audit"].iter_events() if e.event_type == "intent_denied"]
        assert len(denied) == 1
        assert denied[0].payload["reason"] == "PURPOSE_NOT_ALLOWED"


# ---------- 監査チェーン整合性 ----------


class TestAuditChain:
    def test_full_lifecycle_chain_is_valid(self, env):
        eng = env["engine"]
        _mandate(eng, env["escrow"].account_id)
        eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(30), purpose="API_CALL", at=T)
        eng.settle_intent(mandate_id="m1", intent_id="i2", payee_account_id=env["payee_a"].account_id, amount=usdc(90), purpose="API_CALL", at=T)  # denied
        ok, err = env["audit"].verify_chain()
        assert ok, err
        # created + settled + denied = 3 events
        assert env["audit"].event_count() == 3

    def test_engine_works_without_audit_log(self, env):
        # 監査ログ未接続でも決済は機能する（audit_sequence は None）
        eng = IntentSettlementEngine(env["ledger"])  # no audit_log
        _mandate(eng, env["escrow"].account_id)
        r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=env["payee_a"].account_id, amount=usdc(30), purpose="API_CALL", at=T)
        assert r.is_settled
        assert r.audit_sequence is None
