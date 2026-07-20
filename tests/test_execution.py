"""tests/test_execution.py — ③ execution: routing / auction / surplus / arbitrage / proofs。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType, UnbalancedTransactionError
from mandatehub.execution.accounts import ExecutionAccounts
from mandatehub.execution.arbitrage import (
    PoolEdge,
    PoolGraph,
    find_best_arbitrage_cycle,
    record_arbitrage_detection,
)
from mandatehub.execution.auction import SolverBid, run_auction
from mandatehub.execution.proofs import (
    ProofOfBestExecutionGenerator,
    ProofOfSurplusRecaptureGenerator,
    SurplusEvent,
)
from mandatehub.execution.routing import RouteQuote, select_best_route
from mandatehub.execution.surplus import (
    SplitPolicyError,
    SurplusSplitPolicy,
    compute_split,
    post_surplus_split,
)
from mandatehub.transparency.audit_log import AuditLog
from mandatehub.transparency.merkle import verify_proof_with_node_prefix

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestRouting:
    def test_max_net_out_and_tiebreak(self):
        q1 = RouteQuote("rB", ("p",), "USDC", "USDC", 100, 106, 1)  # net 105
        q2 = RouteQuote("rA", ("p",), "USDC", "USDC", 100, 106, 1)  # net 105, tie -> rA
        q3 = RouteQuote("rC", ("p",), "USDC", "USDC", 100, 108, 1)  # net 107 winner
        sel = select_best_route([q1, q2, q3])
        assert sel.winner.route_id == "rC"
        assert sel.reference.route_id == "rA"  # tie broken by id

    def test_min_cost(self):
        q1 = RouteQuote("r1", (), "USDC", "USDC", 100, 90, 0)
        q2 = RouteQuote("r2", (), "USDC", "USDC", 95, 90, 0)
        sel = select_best_route([q1, q2], objective="MIN_COST")
        assert sel.winner.route_id == "r2"

    def test_single_candidate_no_reference(self):
        sel = select_best_route([RouteQuote("r1", (), "USDC", "USDC", 100, 105, 0)])
        assert sel.reference is None

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            select_best_route([])


class TestAuction:
    def test_min_cost_winner_reference_losers(self):
        bids = [
            SolverBid("sB", "i", 95, 0, 0),
            SolverBid("sA", "i", 95, 0, 0),  # tie -> sA
            SolverBid("sC", "i", 90, 0, 0),  # winner
            SolverBid("sD", "i", 99, 0, 0, valid=False),
        ]
        out = run_auction(bids, objective="MIN_COST")
        assert out.winner.solver_id == "sC"
        assert out.reference.solver_id == "sA"
        assert [b.solver_id for b in out.losers] == ["sA", "sB"]
        assert [b.solver_id for b in out.invalid] == ["sD"]

    def test_max_out(self):
        bids = [SolverBid("s1", "i", 0, 100, 0), SolverBid("s2", "i", 0, 120, 0)]
        out = run_auction(bids, objective="MAX_OUT")
        assert out.winner.solver_id == "s2"

    def test_all_invalid_no_winner(self):
        out = run_auction([SolverBid("s1", "i", 90, 0, 0, valid=False)])
        assert out.winner is None


class TestSurplusSplit:
    def test_policy_must_sum_to_10000(self):
        with pytest.raises(SplitPolicyError):
            SurplusSplitPolicy(user_rebate_bps=6000, operator_margin_bps=3000)  # sum 9000

    def test_operator_absorbs_rounding_and_total_exact(self):
        # 決定論的 fuzz: surplus と bps 三つ組を総当り
        for surplus in range(0, 200, 7):
            for rebate_bps in range(0, 10001, 500):
                for ref_bps in range(0, 10001 - rebate_bps, 2500):
                    op_bps = 10000 - rebate_bps - ref_bps
                    pol = SurplusSplitPolicy(
                        user_rebate_bps=rebate_bps, operator_margin_bps=op_bps, referrer_bps=ref_bps
                    )
                    a = compute_split(surplus, pol)
                    assert a.total() == surplus
                    assert all(
                        isinstance(v, int) and v >= 0
                        for v in (a.gas_cents, a.user_rebate_cents, a.operator_margin_cents, a.referrer_cents)
                    )

    def test_gas_clamped_to_surplus(self):
        pol = SurplusSplitPolicy(user_rebate_bps=5000, operator_margin_bps=5000, gas_reimbursement_cents=1000)
        a = compute_split(3, pol)  # gas would be 1000 but surplus only 3
        assert a.gas_cents == 3
        assert a.total() == 3

    def test_zero_surplus_all_zero(self):
        pol = SurplusSplitPolicy(user_rebate_bps=5000, operator_margin_bps=5000)
        a = compute_split(0, pol)
        assert a.total() == 0

    def test_split_structurally_unpostable_if_perturbed(self):
        # 分配は balanced tx として記帳される。1 cent 崩すと UnbalancedTransactionError。
        storage = SQLiteLedgerStorage(":memory:")
        ledger = Ledger(storage)
        src = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "src")
        rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "rebate")
        margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "margin")
        gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")
        b = TransactionBuilder("FUND", "ops", initiated_at=T)
        b.transfer(
            ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "p").account_id, src.account_id, Money.from_units(10, Currency.USDC)
        )
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T)

        accts = ExecutionAccounts(
            payee_account_id="x",
            user_rebate_account_id=rebate.account_id,
            operator_margin_account_id=margin.account_id,
            gas_account_id=gas.account_id,
        )
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
        alloc = compute_split(1000, pol)
        tx = post_surplus_split(
            ledger, surplus_source_account_id=src.account_id, allocation=alloc, accounts=accts,
            currency=Currency.USDC, initiator_id="ops", at=T,
        )
        assert tx is not None
        # 手で不均衡を作ると build 時に弾かれる
        bad = TransactionBuilder("BAD", "ops", initiated_at=T)
        bad.add_entry(src.account_id, Money(cents=-1000, currency=Currency.USDC))
        bad.add_entry(rebate.account_id, Money(cents=999, currency=Currency.USDC))  # 1 cent 欠損
        with pytest.raises(UnbalancedTransactionError):
            bad.build()
        storage.close()


class TestArbitrage:
    def test_profitable_cycle_detected(self):
        g = PoolGraph((PoolEdge("USD", "EUR", 100, 95), PoolEdge("EUR", "USD", 95, 102)))
        opp = find_best_arbitrage_cycle(g, start_currency="USD", start_amount_cents=100, max_cycle_len=3)
        assert opp is not None
        assert opp.cycle == ("USD", "EUR", "USD")
        assert opp.profit_cents == 2

    def test_non_profitable_returns_none(self):
        g = PoolGraph((PoolEdge("USD", "EUR", 100, 95), PoolEdge("EUR", "USD", 95, 98)))
        assert find_best_arbitrage_cycle(g, start_currency="USD", start_amount_cents=100) is None

    def test_floor_never_overclaims(self):
        # 1000 * 95 // 100 = 950; 950 * 102 // 95 = 1020 -> profit 20
        g = PoolGraph((PoolEdge("USD", "EUR", 100, 95), PoolEdge("EUR", "USD", 95, 102)))
        opp = find_best_arbitrage_cycle(g, start_currency="USD", start_amount_cents=1000)
        assert opp.end_amount_cents == 1020

    def test_memo_only_emits_event_no_ledger(self):
        audit = AuditLog(":memory:")
        g = PoolGraph((PoolEdge("USD", "EUR", 100, 95), PoolEdge("EUR", "USD", 95, 102)))
        opp = find_best_arbitrage_cycle(g, start_currency="USD", start_amount_cents=100)
        evt = record_arbitrage_detection(audit, opp, at=T)
        assert evt.event_type == "arbitrage_detected"
        ok, _ = audit.verify_chain()
        assert ok


class TestExecutionProofs:
    def _mk(self):
        bids = [SolverBid("sA", "i1", 95, 0, 0), SolverBid("sB", "i1", 90, 0, 0)]
        auc = run_auction(bids, objective="MIN_COST")
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
        alloc = compute_split(10, pol)  # user_limit 100, executed 90
        return auc, pol, alloc

    def test_best_execution_proof_ok(self):
        auc, pol, alloc = self._mk()
        proof, tree = ProofOfBestExecutionGenerator().generate(
            intent_id="i1", auction=auc, executed_cost_cents=90, user_limit_cents=100,
            in_currency_code="USDC", out_currency_code="USDC", split_policy=pol,
            posted_allocation=alloc, surplus_cents=10, snapshot_at=T,
        )
        assert proof.user_within_limit and proof.user_no_worse_than_best_disclosed
        assert proof.split_matches_policy
        assert verify_proof_with_node_prefix(tree.proof_for("bid:sB"))

    def test_best_execution_catches_wrong_winner_cost(self):
        # executed_cost が実は最良でない（勝者より高い）を主張 → no_worse=False
        auc, pol, alloc = self._mk()
        proof, _ = ProofOfBestExecutionGenerator().generate(
            intent_id="i1", auction=auc, executed_cost_cents=95, user_limit_cents=100,
            in_currency_code="USDC", out_currency_code="USDC", split_policy=pol,
            posted_allocation=alloc, surplus_cents=10, snapshot_at=T,
        )
        assert proof.user_no_worse_than_best_disclosed is False

    def test_best_execution_catches_wrong_split(self):
        auc, pol, _alloc = self._mk()
        wrong = compute_split(10, SurplusSplitPolicy(user_rebate_bps=1000, operator_margin_bps=9000))
        proof, _ = ProofOfBestExecutionGenerator().generate(
            intent_id="i1", auction=auc, executed_cost_cents=90, user_limit_cents=100,
            in_currency_code="USDC", out_currency_code="USDC", split_policy=pol,
            posted_allocation=wrong, surplus_cents=10, snapshot_at=T,
        )
        assert proof.split_matches_policy is False

    def test_best_execution_honors_max_out_objective(self):
        # MAX_OUT: 勝者は出力最大。出力の低い入札を勝者と偽ると no_worse=False。
        from mandatehub.execution.auction import AuctionOutcome
        bids = [SolverBid("sA", "i1", 50, 100, 0), SolverBid("sB", "i1", 40, 90, 0)]
        auc = run_auction(bids, objective="MAX_OUT")  # honest winner sA (output 100)
        pol = SurplusSplitPolicy(user_rebate_bps=10000, operator_margin_bps=0)
        alloc = compute_split(0, pol)
        honest, _ = ProofOfBestExecutionGenerator().generate(
            intent_id="i1", auction=auc, executed_cost_cents=50, user_limit_cents=100,
            in_currency_code="USDC", out_currency_code="JPY", split_policy=pol,
            posted_allocation=alloc, surplus_cents=0, snapshot_at=T,
        )
        assert honest.user_no_worse_than_best_disclosed is True
        # tamper: name sB (lower output) the winner
        tampered = AuctionOutcome(intent_id="i1", objective="MAX_OUT", winner=bids[1], reference=bids[0], losers=(bids[0],), invalid=())
        bad, _ = ProofOfBestExecutionGenerator().generate(
            intent_id="i1", auction=tampered, executed_cost_cents=40, user_limit_cents=100,
            in_currency_code="USDC", out_currency_code="JPY", split_policy=pol,
            posted_allocation=alloc, surplus_cents=0, snapshot_at=T,
        )
        assert bad.user_no_worse_than_best_disclosed is False

    def test_surplus_proof_totals_and_tamper(self):
        pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
        good = compute_split(10, pol)
        proof, _ = ProofOfSurplusRecaptureGenerator().generate(
            surplus_events=[SurplusEvent("e1", 10, good)], snapshot_at=T, currency=Currency.USDC
        )
        assert proof.splits_sum_exact and proof.user_effective_fee_vs_limit_non_positive
        assert proof.total_surplus_cents == 10
        assert proof.total_user_rebate_cents + proof.total_operator_margin_cents == 10
        # 改竄: surplus と分配が食い違う event → splits_sum_exact False
        from mandatehub.execution.surplus import SplitAllocation
        bad = SurplusEvent("e2", 10, SplitAllocation(surplus_cents=10, gas_cents=0, user_rebate_cents=5, operator_margin_cents=3, referrer_cents=0))  # sums 8 != 10
        proof2, _ = ProofOfSurplusRecaptureGenerator().generate(
            surplus_events=[bad], snapshot_at=T, currency=Currency.USDC
        )
        assert proof2.splits_sum_exact is False
