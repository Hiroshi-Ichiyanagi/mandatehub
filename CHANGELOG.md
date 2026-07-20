# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
APIs may change while the project is pre-1.0.

## [0.1.0]

Initial release. Early and unproven; no production adoption.

### Added
- **Vendored verification substrate** (standard library only): append-only double-entry
  ledger with SQLite storage (`mandatehub.core`), Merkle tree, hash-linked tamper-evident
  audit log, and a deterministic as-of commitment (`mandatehub.transparency`).
- **`mandatehub.intent` (4)** — intent/mandate-based autonomous settlement: `Mandate` +
  `IntentSettlementEngine` (budget-bounded M2M settlement re-derived structurally from the
  ledger, every accept/deny on the audit chain), and `ProofOfMandate` /
  `MandatePortfolioProof`. Rich `SpendPolicy` (payee allowlist, per-purpose sub-budgets,
  min/max, deterministic `EpochSpec` velocity/spend caps, rolling windows), session-key
  sub-mandates with provable ancestor non-leakage, atomic batch settlement, lifecycle
  (pause/resume/revoke/top-up/expiry) re-derived from the audit chain, and nonce/replay +
  monotonic-time protection. Canonical `DENIAL_ORDER`.
- **`mandatehub.execution` (3)** — best execution + MEV/arbitrage recapture (standalone;
  imports nothing from `intent`): `select_best_route`, `run_auction`, integer-exact
  `compute_split` (`SurplusSplitPolicy`), memo-only `find_best_arbitrage_cycle`, and
  `ProofOfBestExecution` / `ProofOfSurplusRecapture`.
- **Bridge** `settle_via_auction` / `settle_batch_via_auction` — settle a mandate intent
  through a solver auction and recapture the surplus in one balanced transaction, holding
  every budget invariant (INV-9: budget-side proof fields byte-identical to a plain
  settlement). Cross-currency (Model B) via per-currency venue-clearing accounts.
- **`mandatehub.x402`** — an [x402](https://github.com/coinbase/x402)-compatible facilitator:
  `Facilitator.verify` / `settle`, the `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` /
  `PAYMENT-RESPONSE` header protocol, and the `exact` scheme. Settlement is pluggable via a
  `SettlementAdapter` (default: self-contained ledger settlement, no real money) so a real
  on-chain facilitator can be dropped in. A live HTTP example runs the whole `402 → pay → 200
  + ProofOfMandate` flow. See [docs/X402.md](docs/X402.md) for the phased roadmap.
- Determinism discipline (explicit time only; never `datetime.now()` on a proof/settlement
  path), verified by static (AST) and runtime guards, plus an import-discipline guard that
  `execution/` never imports `intent/`.
- Examples and docs (`docs/INTENT_MANDATES.md`, `docs/EXECUTION_RECAPTURE.md`,
  `docs/ARCHITECTURE.md`); CI on Python 3.11–3.13 with a build/metadata check.

### Notes
- No third-party runtime dependencies (standard library only).
- This project began as two modules inside the `openreserve` verification core and was
  extracted into a fully self-contained, independent project, vendoring the minimal ledger /
  Merkle / audit primitives it needs.
