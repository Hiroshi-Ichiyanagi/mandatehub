# Architecture

mandatehub is a small, self-contained verification core. This document describes how the
pieces fit together, the trust model, and where to extend it.

## Modules

```
mandatehub/
  core/           append-only double-entry ledger + value types + SQLite storage (vendored)
  transparency/   Merkle tree, hash-linked audit log, as-of commitment (vendored)
  intent/         mandate settlement + policy/session-keys/batch/lifecycle + proofs (4)
  execution/      solver auction, surplus recapture, arbitrage attribution + proofs (3)
```

- **core** — `Money`, `Currency`, `Account`, `Transaction`, `TransactionBuilder`, the
  `Ledger`, and a `LedgerStorage` protocol with a `SQLiteLedgerStorage`. Balances are derived
  from settled entries as of a given time. Standard library only.
- **transparency** — `MerkleTree` / `MerkleProof`, the `AuditLog` (hash chain), and
  `audit_root_as_of` (a deterministic as-of commitment). Standard library only.
- **intent (4)** — `Mandate` + `IntentSettlementEngine` settle budget-bounded intents,
  re-deriving all cumulative state structurally from the ledger and logging every accept/deny
  to the `AuditLog`. Composes `SpendPolicy`/`EpochSpec`, session-key sub-mandates, atomic
  batches, lifecycle, and replay/monotonic-time protection. `ProofOfMandateGenerator` /
  `MandatePortfolioProofGenerator` produce offline-verifiable proofs.
  See [INTENT_MANDATES.md](INTENT_MANDATES.md).
- **execution (3)** — standalone (imports nothing from `intent`): `select_best_route`,
  `run_auction`, integer-exact `compute_split`, memo-only `find_best_arbitrage_cycle`, and
  `ProofOfBestExecution` / `ProofOfSurplusRecapture`. See [EXECUTION_RECAPTURE.md](EXECUTION_RECAPTURE.md).

`core/` and `transparency/` are a minimal substrate vendored so the project has **zero
third-party runtime dependencies** and stands entirely on its own.

## Dependency direction

The dependency graph is a one-way DAG:

```
intent.proofs ─┐
intent.mandate ┤
execution ─────┤─> core.ledger ─> core.storage ─> core.types
               │
intent.bridge ─> execution        (one direction only; execution ⊄ intent, enforced by a test)
merkle       (standalone, standard library only)
audit_log    (standalone, standard library only)
audit_query  (audit_log only; shared as-of commitment)
```

`execution` imports nothing from `intent`; the single `intent/bridge.py` seam is the only
place the two meet. There are no cycles and no dependency on any serving layer or external
service.

## Trust model

Inputs: a **ledger state** and an **explicit point in time**. Outputs: artifacts that are
plain data — `to_public_summary()` returns JSON-serializable dicts, inclusion is a
`MerkleProof`, and the audit commitment is `audit_root_as_of(snapshot_at)`. A proof can be
published and re-verified offline by a third party using only this library — no access to the
operator's running system.

The audit log is tamper-evident: each event hashes the previous event's hash, so editing a
past event invalidates every subsequent hash (`verify_chain()`).

The two settlement value planes are kept disjoint and both derived structurally from the
ledger: the **budget** plane (escrow outflow = the user's limit) drives every mandate
invariant; the **receipt** plane (payee credit = executed cost, plus the surplus split)
drives the execution proofs. This is what makes a best-executed settlement byte-identical to a
plain one on the budget side (INV-9) while still recapturing the surplus.

## Determinism

Proof and settlement generation take the as-of / event time as an explicit, required input
and never read the wall clock. Given the same ledger state and the same time, Merkle roots and
audit hashes are identical. `tests/test_superrich_guards.py` pins this with a static (AST)
guard, a runtime guard that makes `datetime.now` raise across the settle/auction/proof paths,
and an import-discipline guard.

## Extension points

- **Storage** — implement the `LedgerStorage` protocol to back the ledger with something
  other than the bundled SQLite store.
- **Execution** — supply quotes/routes/bids and split policies; the auction and split are pure
  deterministic functions over integer inputs.
- **Serving / on-chain anchoring** — intentionally absent; both can be layered on top without
  changing the core.

## What is intentionally out of scope

No HTTP server, no on-chain execution/anchoring, no live DEX or bundler, no external network
or cloud dependency at runtime. mandatehub is the verification core; those belong in layers
built around it.
