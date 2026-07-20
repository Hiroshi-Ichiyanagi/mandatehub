"""Intent-based autonomous settlement under a pre-funded mandate, then prove it.

An orchestrator agent is granted a mandate: a pre-funded budget (escrow), a set of
allowed purposes, a validity window, and a per-transaction cap. It then settles
machine-to-machine intents autonomously — every settlement is checked against the
mandate and recorded on the double-entry ledger; every accept/deny decision is written
to a tamper-evident audit chain. Finally a ProofOfMandate is produced so a third party
can verify offline that the agent never exceeded its budget, and each payee can verify
its own receipts are included without seeing anyone else's.

This is the *verification core* of the "deposit a budget, let the agent spend within
it" model (ERC-4337 / session-key style). The on-chain execution is out of scope; what
is proven here is that spending stayed inside the mandate.

Run: python examples/intent_mandate_settlement.py
"""
from datetime import datetime, timedelta, timezone

from mandatehub import (
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    ProofOfMandateGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
    verify_proof_with_node_prefix,
)
from mandatehub.transparency.audit_log import AuditLog

# Explicit time — proof generation never reads the wall clock.
T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def usdc(units: int) -> Money:
    return Money.from_units(units, Currency.USDC)


def main() -> None:
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")

    platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "mandate-escrow")
    api_a = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider-A")
    api_b = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider-B")

    # 1) Principal pre-funds the escrow with a 100 USDC budget (collateral).
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(platform.account_id, escrow.account_id, usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)

    # 2) Grant a mandate: budget 100, per-tx cap 40, two allowed purposes, 30-day window.
    engine = IntentSettlementEngine(ledger, audit_log=audit)
    engine.create_mandate(
        mandate_id="m1",
        principal_id="agent-orchestrator",
        escrow_account_id=escrow.account_id,
        budget_cap=usdc(100),
        allowed_purposes=frozenset(["API_CALL", "DATA_STREAM"]),
        valid_from=T,
        valid_until=T + timedelta(days=30),
        created_at=T,
        per_transaction_limit=usdc(40),
    )

    # 3) The agent settles a stream of M2M intents autonomously.
    intents = [
        ("i1", api_a, 30, "API_CALL"),      # ok
        ("i2", api_b, 40, "DATA_STREAM"),   # ok  (settled 70, remaining 30)
        ("i3", api_a, 50, "API_CALL"),      # deny: over per-tx cap (40)
        ("i4", api_a, 35, "API_CALL"),      # deny: over remaining budget (30 left)
        ("i5", api_a, 10, "GAMBLING"),      # deny: purpose not allowed
        ("i2", api_a, 5, "API_CALL"),       # deny: duplicate intent id
        ("i6", api_a, 20, "API_CALL"),      # ok  (settled 90, remaining 10)
    ]
    print("--- autonomous settlement stream ---")
    for intent_id, payee, amt, purpose in intents:
        r = engine.settle_intent(
            mandate_id="m1",
            intent_id=intent_id,
            payee_account_id=payee.account_id,
            amount=usdc(amt),
            purpose=purpose,
            at=T,
        )
        tag = "settled" if r.is_settled else f"DENIED ({r.reason})"
        print(f"  {intent_id}: {amt:>3} USDC {purpose:<11} -> {tag}")

    # 4) Produce a mandate proof (deterministic, offline-verifiable).
    proof, tree = ProofOfMandateGenerator(engine).generate("m1", snapshot_at=T)
    summary = proof.to_public_summary()
    print("\n--- proof of mandate (public summary) ---")
    for key in (
        "budget_cap_cents",
        "total_settled_cents",
        "remaining_cents",
        "settlement_count",
        "payee_count",
        "is_within_budget",
        "is_collateralized",
        "escrow_balance_cents",
        "payee_receipts_merkle_root",
    ):
        print(f"  {key}: {summary[key]}")

    # 5) A payee verifies its own receipts are included — without seeing the other's.
    pa = tree.proof_for(api_a.account_id)
    print("\n--- api-provider-A inclusion proof ---")
    print("  received_cents:", pa.leaf.balance_cents)
    print("  verifies:", verify_proof_with_node_prefix(pa))
    print("  root matches published:", pa.root_hash == proof.payee_receipts_root)

    # 6) The audit chain of every accept/deny decision is tamper-evident.
    ok, err = audit.verify_chain()
    print("\n--- audit chain ---")
    print("  events:", audit.event_count(), "(1 created + 3 settled + 4 denied)")
    print("  chain valid:", ok if err is None else f"{ok} ({err})")


if __name__ == "__main__":
    main()
