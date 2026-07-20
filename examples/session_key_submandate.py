"""Session keys / sub-mandates with provable non-leakage.

A root mandate delegates bounded sub-budgets to two sub-agents (session keys). Each
sub-agent spends independently, but the engine re-derives every ancestor's budget from
the ledger on each settlement, so the *combined* spend of all descendants can never
exceed the parent — no matter how the children interleave. The delegation aggregation is
by set-membership on mandate ids, so ids like "a" and "ab" never collide.

Run: python examples/session_key_submandate.py
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
)
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def main() -> None:
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(1000))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "payee")

    eng = IntentSettlementEngine(ledger, audit_log=audit)
    # Root: 100 USDC total, purposes {DATA, COMPUTE}.
    eng.create_mandate(
        mandate_id="root", principal_id="orchestrator", escrow_account_id=escrow.account_id,
        budget_cap=usdc(100), allowed_purposes=frozenset(["DATA", "COMPUTE"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T,
    )
    # Two session keys, each authorized up to 80 (deliberately overlapping headroom).
    for mid, purpose in (("data-agent", "DATA"), ("compute-agent", "COMPUTE")):
        eng.create_sub_mandate(
            parent_mandate_id="root", mandate_id=mid, delegate_id=mid, sub_budget_cap=usdc(80),
            allowed_purposes=frozenset([purpose]), valid_from=T, valid_until=T + timedelta(days=10), created_at=T,
        )

    def settle(mid, iid, amt, purpose):
        r = eng.settle_intent(mandate_id=mid, intent_id=iid, payee_account_id=payee.account_id, amount=usdc(amt), purpose=purpose, at=T)
        print(f"  {mid:<14} {purpose:<8} {amt:>3} USDC -> {'settled' if r.is_settled else 'DENIED (' + r.reason + ')'}")
        return r

    print("--- two session keys drawing on one 100 USDC root ---")
    settle("data-agent", "d1", 60, "DATA")       # ok, root spent 60
    settle("compute-agent", "c1", 50, "COMPUTE")  # DENIED: 60 + 50 = 110 > root 100
    settle("compute-agent", "c2", 40, "COMPUTE")  # ok, root spent 100
    settle("data-agent", "d2", 1, "DATA")         # DENIED: root exhausted

    print("\n--- re-derived aggregates ---")
    print(f"  root subtree spend: {eng.subtree_settled_cents('root', T)/1e6:.0f} USDC  remaining: {eng.remaining_cents('root', T)/1e6:.0f} USDC")
    print(f"  data-agent spend:   {eng.subtree_settled_cents('data-agent', T)/1e6:.0f} USDC")
    print(f"  compute-agent spend:{eng.subtree_settled_cents('compute-agent', T)/1e6:.0f} USDC")

    proof, _ = ProofOfMandateGenerator(eng).generate("root", snapshot_at=T)
    print("\n--- root mandate proof ---")
    print(f"  sub-mandates: {list(proof.sub_mandate_ids)}")
    print(f"  aggregate (incl. descendants): {proof.aggregate_settled_incl_descendants_cents/1e6:.0f} USDC")
    print(f"  within_budget: {proof.is_within_budget}  session_tree_root: {proof.session_tree_root[:16]}...")

    ok, _ = audit.verify_chain()
    print(f"\n  audit chain valid: {ok}")


if __name__ == "__main__":
    main()
