# mandatehub

[![CI](https://github.com/Hiroshi-Ichiyanagi/mandatehub/actions/workflows/ci.yml/badge.svg)](https://github.com/Hiroshi-Ichiyanagi/mandatehub/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mandatehub.svg)](https://pypi.org/project/mandatehub/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

**Site:** <https://mandatehub.ichiyanagi1111.workers.dev> · **PyPI:** `pip install mandatehub`

**Live service:** <https://mandatehub.obolpay.xyz> — a running mandate-gated x402 resource server settling real USDC on Base. Pay it with any x402 client (`python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote`).

A small, self-contained **verification core** for **provable autonomous
machine-to-machine payment**. It does not execute payments on chain — it makes an
autonomous agent's spending **provably bounded, best-executed, and honestly settled**, as
deterministic, offline-verifiable artifacts. It is early and has **no production adoption**
yet — see [Limitations](#limitations).

It covers two directions of always-on M2M value transfer:

- **Intent / account abstraction (4)** — a **mandate** is a pre-funded, budget-bounded
  authorization (the "deposit a budget, let the agent spend within it" model, ERC-4337 /
  session-key style). An autonomous agent settles intents within it, and a `ProofOfMandate`
  lets anyone verify offline that the budget was never exceeded.
- **Best execution / MEV-arbitrage recapture (3)** — a solver auction fills an intent at the
  best disclosed cost, and the price-improvement **surplus** is split (user rebate / operator
  margin / gas) integer-exactly, with a `ProofOfBestExecution` and a
  `ProofOfSurplusRecapture`. "0% fee to the user, yet the system earns."

The two meet in one place — `settle_via_auction` — so a mandate's autonomous spend can be
best-executed **and** the arbitrage recaptured, all provable together.

## Design identity

Every part of mandatehub holds to the same discipline:

- **Deterministic + offline-verifiable.** All proof/settlement generation takes an
  **explicit time** and never reads the wall clock (`datetime.now()`). Same inputs + same
  explicit time → byte-identical hashes, re-verifiable by a third party with only this
  library.
- **Append-only double-entry.** All money is integer minor units (no floats); every
  transaction balances to zero per currency; balances are always derived from settled
  entries.
- **No on-chain execution, no HTTP, no network.** It proves the *accounting* — that whoever
  executed stayed within the mandate and split the surplus honestly. The on-chain pieces
  (bundler / paymaster / session keys / live DEX) live in the layer around it.
- **Standard library only.** Zero third-party runtime dependencies.

## Install

```bash
pip install mandatehub            # published on PyPI
# or, from source:
pip install -e ".[test]"
python -m pytest -q
```

- Python 3.11+ · runtime dependencies: **none** (standard library only).

## Quick start — a budget-bounded mandate

```python
from datetime import datetime, timedelta, timezone
from mandatehub import (
    Currency, IntentSettlementEngine, Ledger, Money, OwnerType,
    ProofOfMandateGenerator, SQLiteLedgerStorage, TransactionBuilder,
)
from mandatehub import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
ledger = Ledger(SQLiteLedgerStorage(":memory:"))
audit = AuditLog(":memory:")

platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
payee = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider")

# Pre-fund the escrow (collateral for the budget).
b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
b.transfer(platform.account_id, escrow.account_id, Money.from_units(100, Currency.USDC))
ledger.post(b.build()); ledger.settle(b.transaction_id, settled_at=T)

eng = IntentSettlementEngine(ledger, audit_log=audit)
eng.create_mandate(
    mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
    budget_cap=Money.from_units(100, Currency.USDC), allowed_purposes=frozenset(["API_CALL"]),
    valid_from=T, valid_until=T + timedelta(days=30), created_at=T,
    per_transaction_limit=Money.from_units(40, Currency.USDC),
)

r = eng.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=payee.account_id,
                      amount=Money.from_units(30, Currency.USDC), purpose="API_CALL", at=T)
assert r.is_settled

proof, tree = ProofOfMandateGenerator(eng).generate("m1", snapshot_at=T)
assert proof.is_within_budget and proof.is_collateralized
```

## Best execution + surplus recapture (the 3 ↔ 4 bridge)

Instead of paying the limit blindly, route the intent through a solver auction and recapture
the price improvement — as **one balanced transaction**, with two proofs:

```python
from mandatehub import ExecutionAccounts, SolverBid, SurplusSplitPolicy, run_auction

accounts = ExecutionAccounts(
    payee_account_id=payee.account_id,
    user_rebate_account_id=rebate.account_id,       # OwnerType.USER
    operator_margin_account_id=margin.account_id,   # OwnerType.FEE (sole residual)
    gas_account_id=gas.account_id,                  # OwnerType.FEE
)
auction = run_auction(
    [SolverBid("solver-B", "i2", fill_cost_cents=Money.from_units(90, Currency.USDC).cents, quoted_out_cents=0, gas_cents=0),
     SolverBid("solver-A", "i2", fill_cost_cents=Money.from_units(95, Currency.USDC).cents, quoted_out_cents=0, gas_cents=0)],
    objective="MIN_COST",
)
res = eng.settle_via_auction(
    mandate_id="m1", intent_id="i2", user_limit=Money.from_units(100, Currency.USDC),
    purpose="API_CALL", at=T, auction=auction,
    split_policy=SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000),
    accounts=accounts,
)
assert res.best_execution.user_within_limit          # user paid <= their limit
assert res.surplus_recapture.splits_sum_exact        # surplus split integer-exact
```

### The two value planes (INV-9)

The load-bearing idea is that a settlement lives on **two disjoint planes**:

| plane | quantity | who reads it |
| ----- | -------- | ------------ |
| **budget** (authorization) | escrow outflow = the user's `user_limit` | mandate budget / velocity / epoch / collateralization |
| **receipt** (execution) | payee credit = `executed_cost`; surplus = `limit − cost` | best-execution + surplus proofs |

Because the budget plane only ever books `user_limit`, a best-executed settlement is
**byte-identical to a plain settlement of the limit** on every budget-side field of
`ProofOfMandate` (**INV-9**) — while the payee legitimately receives the better executed cost
and the surplus is recaptured on the other plane.

## Super-rich capabilities

- **Spend policy** — per-payee allowlist, per-purpose sub-budgets, min/max, deterministic
  **epoch** (integer microseconds) with velocity + spend caps, and rolling-window caps.
- **Session keys / sub-mandates** — a parent delegates a bounded sub-budget; every ancestor's
  budget is re-derived from the ledger, so combined descendant spend can never exceed the
  parent. Aggregation is by set-membership on ids (no substring collisions).
- **Replay & monotonic time** — per-mandate-node nonces, and non-decreasing settlement time so
  a backdated timestamp can't scatter spend across epochs to evade a cap.
- **Atomic batch** settlement; **lifecycle** (pause / resume / revoke / top-up / expiry)
  re-derived from the audit chain.
- **Enriched + portfolio proofs** — per-epoch spend, remaining velocity, session-tree
  commitment, lifecycle state; and a fleet-wide `MandatePortfolioProof` (each escrow group
  individually collateralized).
- **Execution** — deterministic routing + solver auction, integer-exact surplus split (a leak
  is structurally un-postable), memo-only cyclic-arbitrage attribution, cross-currency
  (Model B) settlement via per-currency venue-clearing accounts.

## "Verify me"

Don't take determinism on trust — the suite exercises it, including a runtime guard that
patches `datetime.now` to raise across the settle / auction / proof paths, a static (AST)
guard that no new module reads the wall clock or `total_seconds`, and an import-discipline
guard that `execution/` never imports `intent/`.

```bash
python -m pytest -q
python examples/best_execution_recapture.py     # solver auction + surplus recapture
python examples/session_key_submandate.py       # session-key sub-mandates, non-leakage
python examples/intent_mandate_settlement.py    # budget-bounded settlement + proof
python examples/x402_facilitator.py             # live HTTP 402 flow gated by a mandate
python examples/x402_best_exec.py               # best-exec x402 scheme + offline re-verify
```

## Running it: an x402-compatible facilitator

[x402](https://github.com/coinbase/x402) is Coinbase's HTTP-`402` payment protocol for agents
(live on Base with USDC). mandatehub speaks it as the **mandate + proof layer inside a
facilitator**: `mandatehub.x402` provides a `Facilitator` with `verify` / `settle`, the
`PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` header protocol, and the `exact`
scheme — so a resource server can charge an agent over HTTP 402 while the mandate decides
*whether the agent may pay* and a `ProofOfMandate` rides back in the response.

Settlement is behind a `SettlementAdapter`: today the self-contained ledger adapter (no real
money); swapping in an on-chain adapter (a real x402 facilitator on Base) is the only change
to move real value. `python examples/x402_facilitator.py` runs the whole flow over a real
localhost server. See [docs/X402.md](docs/X402.md) for the phased roadmap to real settlement.

It also speaks a second scheme, **`best-exec`** — ③ exposed as an x402 scheme: the facilitator
best-executes within the resource server's max and rebates the surplus, with both proofs and
offline third-party re-verification in the response. The fixed-value EIP-3009 authorization's
nonce commits to the whole binding, so the operator can't repoint funds or skim the rebate.
Spec: [specs/best-exec.md](specs/best-exec.md); the audited on-chain settler is a hard gate
before real value.

## Layout

```
mandatehub/
  __init__.py     public API re-exports (+ __version__)
  core/           append-only double-entry ledger + value types + SQLite storage (vendored)
  transparency/   Merkle tree + hash-chained audit log + as-of commitment (vendored)
  intent/         mandate settlement + policy/session-keys/batch/lifecycle + proofs (4)
  execution/      solver auction, surplus recapture, arbitrage attribution + proofs (3)
  x402/           x402-compatible facilitator (HTTP 402 verify/settle + mandate gate)
tests/            unit tests + determinism / import-discipline guards
```

`core/` and `transparency/` are a minimal, standard-library-only substrate vendored so the
project is fully independent; `execution/` imports nothing from `intent/` (enforced by a
test), and the single `intent/bridge.py` seam is the only place the two meet.

## Positioning

mandatehub targets **machine-to-machine** payment where an autonomous agent spends a
delegated budget and a counterparty, auditor, or the principal wants to verify — offline,
without trusting the operator — that the agent stayed within its mandate and that any
best-execution surplus was recaptured honestly. It is **not** an on-chain system: the "chain"
here is a local hash-linked audit log, not a distributed ledger. It is **not** a compliance
product and **not** novel cryptography — it is a clean assembly of standard primitives
(double-entry, Merkle trees, hash chains) under a determinism discipline.

## Limitations

- **Determinism is on the time axis.** Identifiers are UUID-based, so independently rebuilt
  ledgers do not share hashes; the guarantee is that a fixed state at a fixed time produces
  identical artifacts.
- **Proof scope is honest, not absolute.** Best-execution proves "best of the *disclosed*
  candidates"; the surplus proof's effective fee is measured "vs the user's own limit". A
  suppressed bid or a fully-colluding solver pool is out of offline scope. Model B proves the
  accounting is honest, not that the swap physically executed on chain.
- **No HTTP / serving layer, no on-chain anchoring** (both can be layered on top).
- **Early and unproven.** v0.1.0, no production adoption, APIs may change.

## Project

- [Site](https://mandatehub.ichiyanagi1111.workers.dev) — the landing page (Cloudflare, auto-deployed from [`site/`](site/) on push to `main`)
- [Architecture](docs/ARCHITECTURE.md) · [Intent mandates](docs/INTENT_MANDATES.md) · [Best execution & recapture](docs/EXECUTION_RECAPTURE.md) · [x402 compatibility & roadmap](docs/X402.md) · [`mandate` model spec](specs/mandate.md) · [`best-exec` scheme spec](specs/best-exec.md)
- [Roadmap](ROADMAP.md) — the public-release + protocol tracks, and the hard gates before mainnet
- [Operations](docs/OPERATIONS.md) — the operating discipline (charter, money-path invariants, staged path to a running facilitator)
- [Releasing](docs/RELEASING.md) — how a version is cut + published to PyPI (trusted publishing; owner setup + per-release checklist)
- [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
