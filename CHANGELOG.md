# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
APIs may change while the project is pre-1.0.

## [Unreleased]

### Added
- **Native rate limiting** — `MANDATEHUB_RATE_PER_MIN` caps the operator's settlements per
  rolling 60s using the mandate's own velocity policy (persisted in `mandate.json`, so it
  survives restarts — re-derived from the ledger, not a process counter). Over-rate calls
  return HTTP 429 `WINDOW_VELOCITY_EXCEEDED`, denied before any facilitator call.
- **Live HTML dashboard** — the operator's `/` serves a server-rendered, no-JS dashboard to
  browsers (settlements, revenue, budget left, per-day table, audit root, how-to-pay) under a
  strict CSP, while API clients still get JSON (content negotiation on `Accept`).
- **Revenue & usage metrics** — the operator serves `GET /metrics` (settlements, revenue,
  unique payers, per-day breakdown) and `deploy/local/stats.py` prints the same report
  offline (`--json` too); one shared computation (`_metrics.py`) keeps the live endpoint and
  the offline report identical. The monitor surfaces `revenue`/`total_settled`.
- **Ops tooling for a live operator** (`deploy/local/`, offline, read-only): `backup.py`
  (WAL-safe online snapshots that self-reject if the audit chain doesn't verify + retention),
  `verify_state.py` (independent re-derivation of budget/collateralization + audit-chain
  verification; representation-robust, value-sensitive tamper detection), `monitor.py`
  (public `/healthz` + agent balance + launchd checks), and hourly/5-min launchd templates.
  Covered by `tests/test_ops_tools.py` (incl. a semantic-tamper detection test).
- **`IntentSettlementEngine.rehydrate_mandate(mandate)`** (H2) — re-attach a mandate to a
  restarted engine over file-backed storage without double history (no new audit event, no
  collateral re-check), with fail-closed guards (double-attach, wrong ledger, child-before-
  parent). Budget, replay, monotonic time, and lifecycle all survive a process restart —
  verified by `tests/test_rehydration.py` and a live SIGKILL-and-replay test on Base Sepolia.
- **Base MAINNET USDC constants, verified on-chain** — `BASE_MAINNET_CHAIN_ID` (8453),
  `BASE_MAINNET_USDC`, `BASE_MAINNET_USDC_DOMAIN` (`{"name": "USD Coin", "version": "2"}` —
  note the domain name differs from Base Sepolia's `"USDC"`; reusing the Sepolia extra on
  mainnet would invalidate every signature). `name()`/`version()`/`DOMAIN_SEPARATOR()` were
  read via `eth_call` and the separator recomputed offline to a byte-exact match.
- `deploy/local/` — the durable **operator** service (file-backed ledger/audit, mandate
  rehydration on boot, `/healthz`, fail-closed on every path) + launchd template + runbook.
- `docs/THREAT_MODEL.md` — H1 preparation: assets, adversaries, defended claims mapped to
  code+tests, known gaps, and a priced audit scope.

- **Coinbase CDP facilitator integration** (`[cdp]` extra) —
  `mandatehub.signers.cdp_header_hook` / `cdp_header_hook_from_file` build the per-request
  Ed25519 JWT (via the official `cdp-sdk`) bound to each endpoint;
  `CDP_FACILITATOR_URL` exported. Confirmed live end-to-end on Base Sepolia:
  `/verify` → `isValid=true`, `/settle` → on-chain transfer. CDP specifics learned live and
  handled: v1 requirements need `description`+`mimeType`; invalid payments come back as
  HTTP 400 **with** a regular result body (the adapter now parses result envelopes on
  non-2xx instead of raising); self-send is rejected.

### Fixed
- `RemoteFacilitatorAdapter` now sends a real `User-Agent` (`mandatehub-x402/1`) by default —
  the stdlib default `Python-urllib/x.y` UA is rejected with HTTP 403 (Cloudflare bot
  protection, error 1010) by public facilitators, observed live against `x402.org`'s
  `/verify`. A `header_hook` can still override it. With this fix, a live no-key wire check
  against the real `x402.org` facilitator round-trips and returns
  `invalid_exact_evm_signature` — confirming the v1 `exact`/EVM payload is parsed end-to-end
  (the P-live wire-format milestone; see `docs/TESTNET.md`).

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
- **Phase 2 — real x402 v1 client** (`mandatehub.x402`): `RemoteFacilitatorAdapter` speaks the
  live facilitator protocol (`/verify`, `/settle`) over `urllib`, and `ExactEvmPayloadBuilder`
  constructs the `exact`-scheme EIP-3009 + EIP-712 payment payload signed by a pluggable
  `Signer`. Base Sepolia constants (chain id 84532, USDC, EIP-712 domain) are built in and
  overridable. Security guards baked in: https-only, cross-host redirect refusal, secret
  redaction, fail-closed on non-2xx/malformed/network errors, tolerant parsing. Real EVM
  signing (`EthAccountSigner`) is isolated behind the optional `[evm]` extra so the core stays
  stdlib-only. Verified against an in-process stub facilitator + `StubSigner` — no network, no
  keys. See [docs/X402.md](docs/X402.md).
- **Phase 3 — `best-exec` scheme** (`mandatehub.x402.best_exec`): exposes best execution +
  surplus recapture (③) as an x402 scheme. A `BestExecFacilitator` (`verify` / `settle`)
  gates a solver auction on a mandate and recaptures the surplus in one balanced settlement,
  reusing `run_auction` / `compute_split` / `settle_via_auction` / `ProofOf*` unchanged. The
  fixed-value EIP-3009 authorization's **nonce commits to the full binding** (settler, payTo,
  rebate/operator sinks, split-policy hash, objective, intent, window, chain), and `verify`
  cross-checks **every** bound field — not just the digest. `verify_best_exec_response` lets a
  third party recompute the accounting from the response alone (auction rerun, independent
  no-worse-than-disclosed, candidates Merkle root, integer-exact split, binding digest = signed
  nonce). Written spec in [specs/best-exec.md](specs/best-exec.md); example
  `examples/x402_best_exec.py`. Offline accounting layer only — the audited on-chain
  `BestExecSettler` contract is out of core and unbuilt (`settlementPlane:"in-ledger"` says so),
  a hard gate before real value.
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
