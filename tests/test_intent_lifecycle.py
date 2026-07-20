"""tests/test_intent_lifecycle.py — pause/resume/revoke/top-up と Ledger.reverse 不使用。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import mandatehub.core.ledger as ledger_mod
from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.intent import IntentSettlementEngine, MandateError, MandateState
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _engine(budget=1000, cap=1000):
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(budget))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m", principal_id="p", escrow_account_id=escrow.account_id,
        budget_cap=usdc(cap), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T,
    )
    return ledger, plat, escrow, pa, eng


def _settle(eng, pa, iid, amt, at):
    return eng.settle_intent(mandate_id="m", intent_id=iid, payee_account_id=pa.account_id, amount=usdc(amt), purpose="A", at=at)


class TestLifecycle:
    def test_pause_resume(self):
        _l, _p, _e, pa, eng = _engine()
        eng.pause_mandate("m", at=T + timedelta(hours=1), reason="risk")
        assert _settle(eng, pa, "i1", 10, T + timedelta(hours=2)).reason == "MANDATE_PAUSED"
        eng.resume_mandate("m", at=T + timedelta(hours=3))
        assert _settle(eng, pa, "i2", 10, T + timedelta(hours=4)).decision == "SETTLED"

    def test_revoke_terminal(self):
        _l, _p, _e, pa, eng = _engine()
        eng.revoke_mandate("m", at=T + timedelta(hours=1), reason="done")
        assert _settle(eng, pa, "i1", 10, T + timedelta(hours=2)).reason == "MANDATE_REVOKED"
        # resume は no-op、状態は REVOKED のまま
        eng.resume_mandate("m", at=T + timedelta(hours=3))
        assert eng.mandate_state("m", T + timedelta(hours=4)).state == MandateState.REVOKED

    def test_pause_on_revoked_raises(self):
        _l, _p, _e, pa, eng = _engine()
        eng.revoke_mandate("m", at=T + timedelta(hours=1), reason="done")
        with pytest.raises(MandateError):
            eng.pause_mandate("m", at=T + timedelta(hours=2), reason="x")

    def test_expire_by_time(self):
        _l, _p, _e, pa, eng = _engine()
        assert _settle(eng, pa, "i1", 10, END + timedelta(seconds=1)).reason == "MANDATE_EXPIRED"

    def test_topup_raises_cap_and_flips_budget(self):
        _l, plat, escrow, pa, eng = _engine(budget=1000, cap=50)
        _settle(eng, pa, "i1", 50, T)
        assert _settle(eng, pa, "i2", 30, T + timedelta(hours=1)).reason == "BUDGET_EXCEEDED"
        tx, evt = eng.top_up_mandate("m", add_collateral=usdc(100), funding_account_id=plat.account_id, at=T + timedelta(hours=2))
        assert eng.effective_cap_cents("m", T + timedelta(hours=3)) == usdc(150).cents
        assert _settle(eng, pa, "i2", 30, T + timedelta(hours=3)).decision == "SETTLED"

    def test_backdated_resume_does_not_defeat_pause(self):
        # pause(ts=T+20) の後に resume(ts=T+10) をバックデートで追記しても、時刻順で
        # pause が「時間的に最後」なので T+25 の状態は PAUSED のまま（順序ガード）。
        _l, _p, _e, pa, eng = _engine()
        eng.pause_mandate("m", at=T + timedelta(hours=20), reason="risk")
        eng.resume_mandate("m", at=T + timedelta(hours=10))  # backdated before the pause
        assert eng.mandate_state("m", T + timedelta(hours=25)).state == MandateState.PAUSED
        assert _settle(eng, pa, "i1", 10, T + timedelta(hours=25)).reason == "MANDATE_PAUSED"

    def test_state_rederived_at_various_times(self):
        _l, _p, _e, pa, eng = _engine()
        eng.pause_mandate("m", at=T + timedelta(hours=5), reason="x")
        assert eng.mandate_state("m", T + timedelta(hours=1)).state == MandateState.ACTIVE  # before pause
        assert eng.mandate_state("m", T + timedelta(hours=6)).state == MandateState.PAUSED

    def test_ledger_reverse_never_called(self, monkeypatch):
        # ライフサイクル・決済のどの経路でも Ledger.reverse（now() をハードコード）を呼ばない
        def _boom(*a, **k):
            raise AssertionError("Ledger.reverse must never be called on a deterministic path")

        monkeypatch.setattr(ledger_mod.Ledger, "reverse", _boom)
        _l, plat, escrow, pa, eng = _engine(cap=100)
        _settle(eng, pa, "i1", 10, T)
        eng.pause_mandate("m", at=T + timedelta(hours=1), reason="x")
        eng.resume_mandate("m", at=T + timedelta(hours=2))
        eng.top_up_mandate("m", add_collateral=usdc(50), funding_account_id=plat.account_id, at=T + timedelta(hours=3))
        _settle(eng, pa, "i2", 10, T + timedelta(hours=4))
        eng.revoke_mandate("m", at=T + timedelta(hours=5), reason="done")
