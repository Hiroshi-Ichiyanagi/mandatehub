"""tests/test_intent_bridge.py — ③↔④ 橋渡し settle_via_auction（crown）と INV-9 / Model B。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.execution import ExecutionAccounts, SolverBid, SurplusSplitPolicy, run_auction
from mandatehub.intent import IntentRequest, IntentSettlementEngine, ProofOfMandateGenerator
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _fund(ledger, src, dst, money, at=T):
    b = TransactionBuilder("FUND", "ops", initiated_at=at)
    b.transfer(src.account_id, dst.account_id, money)
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=at)


def _model_a():
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    _fund(ledger, plat, escrow, usdc(1000))
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "payee")
    rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "rebate")
    margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "margin")
    gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(mandate_id="m", principal_id="agent", escrow_account_id=escrow.account_id, budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]), valid_from=T, valid_until=END, created_at=T)
    accts = ExecutionAccounts(payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id, operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id)
    return ledger, audit, eng, escrow, payee, rebate, margin, gas, accts


def _auc(fill_costs):
    bids = [SolverBid(f"s{i}", "i1", fc, 0, 0) for i, fc in enumerate(fill_costs)]
    return run_auction(bids, objective="MIN_COST")


class TestModelACrown:
    def test_happy_path_balanced_and_split(self):
        ledger, audit, eng, escrow, payee, rebate, margin, gas, accts = _model_a()
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000, gas_reimbursement_cents=usdc(1).cents)
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([usdc(90).cents, usdc(95).cents]), split_policy=pol, accounts=accts)
        assert res.settlement.is_settled
        assert res.executed_cost_cents == usdc(90).cents
        # budget plane: escrow outflow == user_limit
        assert usdc(1000).cents - ledger.balance(escrow.account_id, as_of=T).cents == usdc(100).cents
        # receipt plane: payee gets executed_cost; surplus split
        assert ledger.balance(payee.account_id, as_of=T) == usdc(90)
        assert ledger.balance(rebate.account_id, as_of=T).cents + ledger.balance(margin.account_id, as_of=T).cents + ledger.balance(gas.account_id, as_of=T).cents == usdc(10).cents
        # proofs
        assert res.best_execution.user_within_limit and res.best_execution.split_matches_policy
        assert res.surplus_recapture.splits_sum_exact and res.surplus_recapture.user_effective_fee_vs_limit_non_positive
        ok, _ = audit.verify_chain()
        assert ok
        # 3 audit events for this settlement: intent_settled, best_execution, surplus_recaptured
        types = [e.event_type for e in audit.iter_events()]
        assert types.count("intent_settled") == 1 and "best_execution" in types and "surplus_recaptured" in types

    def test_inv9_budget_side_identical_to_plain(self):
        # settle_via_auction と settle_intent（同じ user_limit）で予算側フィールドが byte-identical
        la, aa, ea, esa, pa, ra, ma, ga, aca = _model_a()
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
        ea.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([usdc(90).cents]), split_policy=pol, accounts=aca)
        proof_auc, _ = ProofOfMandateGenerator(ea).generate("m", snapshot_at=T)

        lp = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = lp.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        escrowp = lp.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
        _fund(lp, plat, escrowp, usdc(1000))
        payeep = lp.open_account(OwnerType.USER, Currency.USDC, "payee")
        engp = IntentSettlementEngine(lp, audit_log=AuditLog(":memory:"))
        engp.create_mandate(mandate_id="m", principal_id="agent", escrow_account_id=escrowp.account_id, budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]), valid_from=T, valid_until=END, created_at=T)
        engp.settle_intent(mandate_id="m", intent_id="i1", payee_account_id=payeep.account_id, amount=usdc(100), purpose="SWAP", at=T)
        proof_plain, _ = ProofOfMandateGenerator(engp).generate("m", snapshot_at=T)

        budget = lambda p: (p.total_settled_cents, p.remaining_cents, p.is_within_budget, p.is_collateralized, p.escrow_balance_cents, p.settlement_count)
        assert budget(proof_auc) == budget(proof_plain)
        assert proof_auc.payee_receipts_root != proof_plain.payee_receipts_root  # receipt differs

    def test_no_winning_bid_denied_no_write(self):
        ledger, audit, eng, escrow, *_rest, accts = _model_a()
        before = sum(1 for _ in ledger.iter_all_transactions())
        auc = run_auction([SolverBid("s", "i1", usdc(90).cents, 0, 0, valid=False)], objective="MIN_COST")
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=auc, split_policy=SurplusSplitPolicy(user_rebate_bps=10000, operator_margin_bps=0), accounts=accts)
        assert res.reason == "NO_WINNING_BID"
        assert sum(1 for _ in ledger.iter_all_transactions()) == before

    def test_execution_above_limit_denied(self):
        ledger, audit, eng, escrow, *_rest, accts = _model_a()
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([usdc(120).cents]), split_policy=SurplusSplitPolicy(user_rebate_bps=10000, operator_margin_bps=0), accounts=accts)
        assert res.reason == "EXECUTION_ABOVE_LIMIT"

    def test_gas_exceeds_surplus_denied(self):
        ledger, audit, eng, escrow, *_rest, accts = _model_a()
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000, gas_reimbursement_cents=usdc(50).cents)
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([usdc(90).cents]), split_policy=pol, accounts=accts)  # surplus 10 < gas 50
        assert res.reason == "GAS_EXCEEDS_SURPLUS"

    def test_mandate_denial_takes_precedence(self):
        ledger, audit, eng, escrow, *_rest, accts = _model_a()
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="NOT_ALLOWED", at=T, auction=_auc([usdc(90).cents]), split_policy=SurplusSplitPolicy(user_rebate_bps=10000, operator_margin_bps=0), accounts=accts)
        assert res.reason == "PURPOSE_NOT_ALLOWED"

    def test_zero_cost_execution_denied_ledger_stays_readable(self):
        # executed_cost 0 は payee credit 0 になり構造リーダーを永久に fail-closed に
        # する。非正の執行は却下し、元帳は読めるまま。
        ledger, audit, eng, escrow, *_rest, accts = _model_a()
        before = sum(1 for _ in ledger.iter_all_transactions())
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([0]), split_policy=SurplusSplitPolicy(user_rebate_bps=10000, operator_margin_bps=0), accounts=accts)
        assert res.reason == "NON_POSITIVE_EXECUTION"
        assert sum(1 for _ in ledger.iter_all_transactions()) == before
        assert eng.remaining_cents("m", T) == usdc(1000).cents  # ledger still readable

    def test_payee_aliasing_rejected(self):
        from mandatehub.intent.errors import MandateError
        ledger, audit, eng, escrow, payee, rebate, margin, gas, accts = _model_a()
        bad = ExecutionAccounts(payee_account_id=rebate.account_id, user_rebate_account_id=rebate.account_id, operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id)
        with pytest.raises(MandateError):
            eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=_auc([usdc(90).cents]), split_policy=SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000), accounts=bad)


class TestModelB:
    def _setup(self):
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        audit = AuditLog(":memory:")
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
        _fund(ledger, plat, escrow, usdc(1000))
        venue_in = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "venue_in")
        venue_out = ledger.open_account(OwnerType.CLEARING, Currency.JPY, "venue_out")
        payee = ledger.open_account(OwnerType.USER, Currency.JPY, "payee")
        rebate = ledger.open_account(OwnerType.USER, Currency.JPY, "rebate")
        margin = ledger.open_account(OwnerType.FEE, Currency.JPY, "margin")
        gas = ledger.open_account(OwnerType.FEE, Currency.JPY, "gas")
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        eng.create_mandate(mandate_id="m", principal_id="agent", escrow_account_id=escrow.account_id, budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]), valid_from=T, valid_until=END, created_at=T)
        accts = ExecutionAccounts(payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id, operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id, venue_clearing_account_id=venue_in.account_id, venue_clearing_out_account_id=venue_out.account_id)
        return ledger, audit, eng, escrow, venue_in, venue_out, payee, accts

    def _jpy_auc(quoted_out):
        pass

    def test_cross_currency_balanced_and_budget_plane(self):
        ledger, audit, eng, escrow, venue_in, venue_out, payee, accts = self._setup()
        # user pays up to 100 USDC, wants at least 14000 JPY; solver delivers 15000 JPY
        bids = [SolverBid("s1", "i1", usdc(100).cents, 15000, 0)]
        auc = run_auction(bids, objective="MAX_OUT")
        pol = SurplusSplitPolicy(user_rebate_bps=6000, operator_margin_bps=4000)
        res = eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=auc, split_policy=pol, accounts=accts, quoted_user_out=Money(cents=14000, currency=Currency.JPY))
        assert res.settlement.is_settled
        # budget plane (C_in): escrow outflow == user_limit
        assert usdc(1000).cents - ledger.balance(escrow.account_id, as_of=T).cents == usdc(100).cents
        # payee got quoted_user_out (C_out); surplus 1000 JPY split
        assert ledger.balance(payee.account_id, as_of=T).cents == 14000
        assert res.split.surplus_cents == 1000
        # venue mirrors: +user_limit USDC / -executed_out JPY (legitimate)
        assert ledger.balance(venue_in.account_id, as_of=T) == usdc(100)
        assert ledger.balance(venue_out.account_id, as_of=T).cents == -15000
        ok, _ = audit.verify_chain()
        assert ok

    def test_missing_venue_out_raises(self):
        ledger, audit, eng, escrow, venue_in, venue_out, payee, accts = self._setup()
        bad = ExecutionAccounts(payee_account_id=accts.payee_account_id, user_rebate_account_id=accts.user_rebate_account_id, operator_margin_account_id=accts.operator_margin_account_id, gas_account_id=accts.gas_account_id, venue_clearing_account_id=venue_in.account_id)
        from mandatehub.intent.errors import MandateError
        bids = [SolverBid("s1", "i1", usdc(100).cents, 15000, 0)]
        with pytest.raises(MandateError):
            eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=run_auction(bids, objective="MAX_OUT"), split_policy=SurplusSplitPolicy(user_rebate_bps=6000, operator_margin_bps=4000), accounts=bad, quoted_user_out=Money(cents=14000, currency=Currency.JPY))


class TestBatchViaAuction:
    def test_atomic_best_ex_batch(self):
        ledger, audit, eng, escrow, payee, rebate, margin, gas, accts = _model_a()
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
        legs = [
            (IntentRequest("i1", payee.account_id, usdc(100), "SWAP"), _auc([usdc(90).cents]), None),
            (IntentRequest("i2", payee.account_id, usdc(80), "SWAP"), run_auction([SolverBid("s", "i2", usdc(70).cents, 0, 0)], objective="MIN_COST"), None),
        ]
        result, bestex, surplus = eng.settle_batch_via_auction(mandate_id="m", legs=legs, split_policy=pol, accounts=accts, at=T)
        assert result.decision == "SETTLED"
        assert len(bestex) == 2
        assert surplus.total_surplus_cents == usdc(10).cents + usdc(10).cents
        # budget plane: total escrow outflow == 100 + 80
        assert usdc(1000).cents - ledger.balance(escrow.account_id, as_of=T).cents == usdc(180).cents
        # payee got executed costs 90 + 70 = 160
        assert ledger.balance(payee.account_id, as_of=T).cents == usdc(160).cents

    def test_cross_currency_leg_denied_no_write(self):
        # バッチ最良執行は Model A のみ。クロス通貨レグは fail-closed で却下し、何も書かない。
        ledger, audit, eng, escrow, payee, rebate, margin, gas, accts = _model_a()
        before = sum(1 for _ in ledger.iter_all_transactions())
        legs = [(IntentRequest("i1", payee.account_id, usdc(100), "SWAP"), _auc([usdc(90).cents]), Money(cents=14000, currency=Currency.JPY))]
        result, bx, sp = eng.settle_batch_via_auction(mandate_id="m", legs=legs, split_policy=SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000), accounts=accts, at=T)
        assert result.decision == "DENIED"
        assert "CROSS_CURRENCY_NOT_SUPPORTED_IN_BATCH" in result.reason
        assert sum(1 for _ in ledger.iter_all_transactions()) == before
