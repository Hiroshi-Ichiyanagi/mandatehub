"""The `best-exec` x402 scheme: best-execute within the user's max, rebate the surplus.

A resource server charges "up to 100 USDC" (`maxAmountRequired`). Instead of paying the
ceiling blindly (the `exact` scheme), the facilitator runs a solver auction, fills at the
best disclosed cost, and rebates the price-improvement surplus to the user — with a
`ProofOfBestExecution` and a `ProofOfSurplusRecapture` riding back in the response. The
agent's authorization is a *fixed-value* EIP-3009 whose nonce is a commitment over the full
best-exec binding (settler, payTo, split policy, objective, window), so the facilitator can
neither repoint the funds nor skim the rebate without invalidating the signature.

This is the offline accounting layer (stub verifier + in-ledger settlement, no network, no
keys, no on-chain settler) — it proves the *accounting*. Moving real value needs an audited
BestExecSettler contract and the mainnet gates; see specs/best-exec.md and docs/X402.md.

Run: python examples/x402_best_exec.py
"""
from datetime import datetime, timedelta, timezone

from mandatehub import (
    AuditLog,
    Currency,
    ExecutionAccounts,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    SolverBid,
    SQLiteLedgerStorage,
    SurplusSplitPolicy,
    TransactionBuilder,
)
from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    BestExecFacilitator,
    BestExecParams,
    X402PaymentRequirements,
    build_best_exec_payload,
    verify_best_exec_response,
)

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
    gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")

    # Pre-fund the escrow (collateral for a 1000 USDC budget).
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(1000))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)

    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
        budget_cap=usdc(1000), allowed_purposes=frozenset(["X402_BEST_EXEC"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T,
    )
    accounts = ExecutionAccounts(
        payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id,
        operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id,
    )

    # The resource server's best-exec terms: 70% of the surplus rebated to the user.
    policy = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)
    be = BestExecParams(
        objective="MIN_COST", settler="0xBestExecSettler", split_policy=policy,
        rebate_to=rebate.account_id, operator_to=margin.account_id,
        mandate_ref="m1", purpose="X402_BEST_EXEC",
    )
    requirements = X402PaymentRequirements(
        scheme="best-exec", network="base-sepolia",
        max_amount_required=str(usdc(100).cents),      # charge "up to 100 USDC"
        asset=BASE_SEPOLIA_USDC, pay_to=payee.account_id,
        resource="https://api.example/execute", max_timeout_seconds=60,
        extra={"name": "USDC", "version": "2", "bestExec": be.to_wire()},
    )

    facilitator = BestExecFacilitator(eng, "m1", accounts, network="base-sepolia")

    # The agent signs a fixed-value authorization whose nonce commits to the whole binding.
    payload = build_best_exec_payload(requirements, from_addr="0xAgent", at=T, intent_id="i1")

    # Solvers disclose their fills; the auction is deterministic (min cost, tie-break by id).
    bids = [
        SolverBid("solver-A", "i1", fill_cost_cents=usdc(96).cents, quoted_out_cents=0, gas_cents=0),
        SolverBid("solver-B", "i1", fill_cost_cents=usdc(90).cents, quoted_out_cents=0, gas_cents=0),
        SolverBid("solver-C", "i1", fill_cost_cents=usdc(93).cents, quoted_out_cents=0, gas_cents=0),
    ]

    ok, reason = facilitator.verify(requirements, payload, bids, at=T)
    print(f"--- verify ---\n  ok={ok}  reason={reason}")

    result = facilitator.settle(requirements, payload, bids, at=T)
    r = result.response
    print("\n--- settle (best-exec) ---")
    print(f"  ceiling (maxAmount): {int(r['maxAmount'])/1e6:.2f} USDC")
    print(f"  winner: {r['auction']['winnerId']}  executed cost: {int(r['executedCost'])/1e6:.2f} USDC")
    print(f"  surplus recaptured: {int(r['split']['surplus'])/1e6:.2f} USDC")
    print(f"    user rebate:     {int(r['split']['user_rebate'])/1e6:.4f} USDC")
    print(f"    operator margin: {int(r['split']['operator_margin'])/1e6:.4f} USDC")
    print(f"  settlement plane: {r['settlementPlane']}  (in-ledger — no real money; audited settler is out of core)")

    print("\n--- ledger (two value planes) ---")
    spent = usdc(1000).cents - ledger.balance(escrow.account_id, as_of=T).cents
    print(f"  BUDGET plane  — escrow outflow: {spent/1e6:.2f} USDC (== the user's max, INV-9)")
    print(f"  RECEIPT plane — payee received: {ledger.balance(payee.account_id, as_of=T).cents/1e6:.2f} USDC (== executed cost)")
    print(f"                  user rebate:    {ledger.balance(rebate.account_id, as_of=T).cents/1e6:.4f} USDC")

    # A third party re-verifies the accounting from the response alone (stdlib only).
    print("\n--- offline re-verification (third party, response only) ---")
    checks = verify_best_exec_response(r, agreed_policy=policy)
    for name, passed in checks.items():
        print(f"  {'OK ' if passed else 'XX '} {name}")
    print(f"\n  all accounting checks pass: {all(checks.values())}")
    print("  (step 9 — confirming the funds moved on chain — is one online tx read, out of offline scope)")

    ok, err = audit.verify_chain()
    print(f"\n  audit chain valid: {ok if err is None else f'{ok} ({err})'}  ({audit.event_count()} events)")


if __name__ == "__main__":
    main()
