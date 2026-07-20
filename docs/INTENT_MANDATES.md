# Intent-based mandates

The `intent` module is the **verification core** for the "deposit a budget once, let an
autonomous agent spend within it" model — the account-abstraction / session-key style of
machine-to-machine payment (ERC-4337, EIP-7702, intents/solvers). mandatehub does not
execute the payment; it makes the spending **provably bounded**.

## The trust problem it solves

In an intent-based system a principal grants an autonomous agent a **mandate**: a
pre-funded budget, a set of allowed purposes, a validity window, and a per-transaction
cap. The agent then settles intents on its own, 24/7, with no human in the loop. The
obvious question a counterparty, auditor, or the principal will ask is:

> How do I know the agent never spent outside the mandate?

`intent` answers it with the same discipline as the rest of mandatehub: an append-only
double-entry ledger, a tamper-evident audit chain, a Merkle commitment, and strict
determinism — so a third party can re-verify **offline** that the budget was never
exceeded, without trusting the operator.

## Model

- **Mandate** — `mandate_id`, `budget_cap`, `allowed_purposes`, `[valid_from, valid_until]`,
  optional `per_transaction_limit`, and an `escrow_account_id`. A mandate is created only
  if the escrow is **fully collateralized** (`escrow balance >= budget_cap`): deposit
  first, spend within the deposit.
- **IntentSettlementEngine** — for each intent it checks the static rules (window,
  purpose, per-tx cap, currency, sign), rejects duplicate intent ids, and enforces the
  **cumulative budget** (`amount <= budget_cap - already_settled`). The cumulative figure
  is always **re-derived from the ledger**, never a side counter — so a counter/ledger
  drift is structurally impossible. A pass posts an `escrow -> payee` settled transfer; a
  fail writes nothing to the ledger. Either way the decision is appended to the audit
  chain (`intent_settled` / `intent_denied`) with an explicit timestamp.
- **ProofOfMandate** — a point-in-time artifact carrying the budget invariant
  (`is_within_budget = remaining >= 0`), the collateralization check
  (`is_collateralized = escrow_balance >= remaining`), a Merkle root over per-payee
  receipts, and the audit-chain commitment. Each payee can verify its own receipts are
  included without seeing anyone else's, exactly like the proof-of-reserves inclusion
  proof.

Denial reason codes: `OUTSIDE_WINDOW`, `PURPOSE_NOT_ALLOWED`, `PER_TX_LIMIT_EXCEEDED`,
`BUDGET_EXCEEDED`, `DUPLICATE_INTENT`, `NON_POSITIVE_AMOUNT`, `CURRENCY_MISMATCH`.

## Super-rich capabilities

Beyond the single fixed budget, a mandate composes:

- **Spend policy** (`SpendPolicy`) — per-payee allowlist, per-purpose sub-budgets, min/max
  per-tx, and **velocity / spend caps per deterministic epoch** (`EpochSpec`, integer
  microseconds — no wall clock) and per rolling window.
- **Session keys / sub-mandates** — a parent delegates a bounded sub-budget to a
  sub-agent; every ancestor's budget is re-derived from the ledger on each settlement, so
  combined descendant spend can never exceed the parent (`PARENT_BUDGET_EXCEEDED`).
  Aggregation is by set-membership on ids, so `a` and `ab` never collide.
- **Replay & monotonic time** — optional per-mandate-node nonces (a re-signed intent with a
  fresh id is still caught), and settlement time must be non-decreasing per mandate so a
  backdated `at` can't scatter spend across epochs to evade a cap.
- **Batch settlement** — many intents in one atomic transaction (all-or-nothing).
- **Lifecycle** — `pause` / `resume` / `revoke` / `top_up` / expiry, each an audit event;
  state (and effective cap after top-ups) is re-derived from the chain, never stored.
- **Best-execution bridge** — `settle_via_auction` routes an intent through a solver
  auction and recaptures the price-improvement surplus, all provable, with every budget
  invariant preserved (**INV-9**). See [EXECUTION_RECAPTURE.md](EXECUTION_RECAPTURE.md).
- **Enriched + portfolio proofs** — `ProofOfMandate` carries per-epoch spend, remaining
  velocity, the session-tree commitment, and lifecycle state; `MandatePortfolioProof`
  proves a whole fleet is within budget and collateralized (shared escrow counted once).

The denial ladder is a pinned canonical order (`DENIAL_ORDER`) so the first failing rule
always wins and is hash-committed to the audit chain.

## Usage

```python
from datetime import datetime, timedelta, timezone
from mandatehub import (
    Currency, Ledger, Money, OwnerType, SQLiteLedgerStorage, TransactionBuilder,
    IntentSettlementEngine, ProofOfMandateGenerator,
)
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
ledger = Ledger(SQLiteLedgerStorage(":memory:"))
audit = AuditLog(":memory:")

platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
escrow = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "escrow")
payee = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider")

# Pre-fund the escrow with a 100 USDC budget.
b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
b.transfer(platform.account_id, escrow.account_id, Money.from_units(100, Currency.USDC))
ledger.post(b.build()); ledger.settle(b.transaction_id, settled_at=T)

engine = IntentSettlementEngine(ledger, audit_log=audit)
engine.create_mandate(
    mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
    budget_cap=Money.from_units(100, Currency.USDC),
    allowed_purposes=frozenset(["API_CALL"]),
    valid_from=T, valid_until=T + timedelta(days=30), created_at=T,
    per_transaction_limit=Money.from_units(40, Currency.USDC),
)

r = engine.settle_intent(
    mandate_id="m1", intent_id="i1", payee_account_id=payee.account_id,
    amount=Money.from_units(30, Currency.USDC), purpose="API_CALL", at=T,
)
assert r.is_settled

proof, tree = ProofOfMandateGenerator(engine).generate("m1", snapshot_at=T)
assert proof.is_within_budget and proof.is_collateralized
```

A runnable end-to-end version (with several accept/deny paths and a payee inclusion
proof) is in [`examples/intent_mandate_settlement.py`](../examples/intent_mandate_settlement.py).

## Determinism

Like the other proof generators, `ProofOfMandateGenerator.generate()` requires an
explicit `snapshot_at` and never reads the wall clock; settlement uses an explicit `at`
for both the ledger entry and the audit event. The same ledger state at the same time
reproduces the same mandate proof.

## Where this sits among M2M payment designs

Autonomous value-transfer systems point in different directions — continuous
**streaming** (Superfluid/Sablier/L402), resource **barter** (DePIN-style netting),
**arbitrage/MEV recapture**, and **intent / account-abstraction** budgets. The
intent/mandate direction is the one this module models, because its hard part is a
verification problem — *prove the delegated agent stayed inside its budget* — which is
squarely what mandatehub is for. A well-built intent/solver layer also subsumes the
arbitrage direction: the solver competing to fill an intent captures the routing spread,
so the profit engine of "arbitrage recapture" lives *inside* the intent frame while the
budget stays provably bounded here.

## Out of scope (unchanged)

The on-chain pieces — a bundler/paymaster, session keys, solver routing — live in the
execution layer around mandatehub, not in it. This module proves the accounting: that
whatever executed stayed within the mandate.
