"""Best-execution + surplus recapture: the (3)<->(4) bridge, provable end-to-end.

A mandate authorizes an agent to spend up to a per-intent limit. Instead of paying the
limit blindly, the agent runs a solver auction, fills the intent at the *best* quoted
cost, and the price-improvement surplus is split by policy (user rebate / operator margin
/ gas) via a single balanced transaction. Two proofs come out: the user got best of the
disclosed quotes and stayed within their limit; and the surplus was recaptured and split
integer-exactly with the user's effective fee <= 0.

The key property (INV-9): the mandate's *budget* accounting only ever sees the user's
limit (the escrow outflow), so it is byte-identical to a plain settlement — while the
payee genuinely receives less and the difference is recaptured. "0% fee to the user, yet
the system earns", all offline-verifiable.

Run: python examples/best_execution_recapture.py
"""
from datetime import datetime, timedelta, timezone

from mandatehub import (
    Currency,
    ExecutionAccounts,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    ProofOfMandateGenerator,
    SolverBid,
    SQLiteLedgerStorage,
    SurplusSplitPolicy,
    TransactionBuilder,
    run_auction,
)
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def main() -> None:
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")

    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "mandate-escrow")
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "liquidity-venue")
    rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "user-rebate")
    margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "operator-margin")
    gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas-reimbursement")

    # Pre-fund the escrow (collateral for a 1000 USDC budget).
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(1000))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)

    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
        budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T,
    )
    accounts = ExecutionAccounts(
        payee_account_id=payee.account_id,
        user_rebate_account_id=rebate.account_id,
        operator_margin_account_id=margin.account_id,
        gas_account_id=gas.account_id,
    )
    # 70% of the price-improvement goes back to the user, 30% to the operator, gas off-top.
    policy = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000, gas_reimbursement_cents=usdc(1).cents)

    # Three solvers bid to fill; the auction is deterministic (min cost, tie-break by id).
    bids = [
        SolverBid("solver-A", "i1", fill_cost_cents=usdc(96).cents, quoted_out_cents=0, gas_cents=0),
        SolverBid("solver-B", "i1", fill_cost_cents=usdc(90).cents, quoted_out_cents=0, gas_cents=0),
        SolverBid("solver-C", "i1", fill_cost_cents=usdc(93).cents, quoted_out_cents=0, gas_cents=0),
    ]
    auction = run_auction(bids, objective="MIN_COST")

    res = eng.settle_via_auction(
        mandate_id="m1", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T,
        auction=auction, split_policy=policy, accounts=accounts,
    )

    print("--- best-execution settlement ---")
    print(f"  winner: {auction.winner.solver_id}  executed cost: {res.executed_cost_cents/1e6:.2f} USDC")
    print(f"  user limit: 100.00 USDC  ->  surplus recaptured: {res.split.surplus_cents/1e6:.2f} USDC")
    print(f"    user rebate:     {res.split.user_rebate_cents/1e6:.4f} USDC")
    print(f"    operator margin: {res.split.operator_margin_cents/1e6:.4f} USDC")
    print(f"    gas:             {res.split.gas_cents/1e6:.4f} USDC")

    print("\n--- ledger (two value planes) ---")
    print(f"  BUDGET plane  — escrow outflow: {(usdc(1000).cents - ledger.balance(escrow.account_id, as_of=T).cents)/1e6:.2f} USDC (== user limit)")
    print(f"  RECEIPT plane — payee received: {ledger.balance(payee.account_id, as_of=T).cents/1e6:.2f} USDC (== executed cost)")

    print("\n--- proofs ---")
    be = res.best_execution
    print(f"  best-execution: within_limit={be.user_within_limit}  no_worse_than_best_disclosed={be.user_no_worse_than_best_disclosed}  split_matches_policy={be.split_matches_policy}")
    sr = res.surplus_recapture
    print(f"  surplus recapture: splits_sum_exact={sr.splits_sum_exact}  user_effective_fee<=0={sr.user_effective_fee_vs_limit_non_positive}")

    # INV-9: the mandate's budget accounting is identical to a plain settlement of the limit.
    proof, _tree = ProofOfMandateGenerator(eng).generate("m1", snapshot_at=T)
    print("\n--- mandate proof (budget side) ---")
    print(f"  total_settled: {proof.total_settled_cents/1e6:.2f} USDC (the authorized limit, not the executed cost)")
    print(f"  remaining: {proof.remaining_cents/1e6:.2f} USDC  within_budget={proof.is_within_budget}  collateralized={proof.is_collateralized}")

    ok, err = audit.verify_chain()
    print(f"\n  audit chain valid: {ok if err is None else f'{ok} ({err})'}  ({audit.event_count()} events)")


if __name__ == "__main__":
    main()
