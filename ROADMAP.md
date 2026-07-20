# Roadmap

These are **directions, not promises**. mandatehub is early and exploratory; there is no
production adoption and APIs may change. The plan below mirrors how x402 went public — an
open protocol with a spec, SDK, docs, examples, and a reference facilitator — scaled to an
independent, early project, and **honest about the hard gates before any real money moves**.

Two tracks run in parallel: **Release** (make the open-source project public — safe, no real
value) and **Protocol** (mature the payment path — testnet first, mainnet last). The operating
discipline for both — and the staged path from this library to a running facilitator — is in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Done (the verification core)

- **Intent / account abstraction (4)** — `Mandate` + `IntentSettlementEngine`, spend policy,
  session keys, batch, lifecycle, replay/monotonic-time, `ProofOfMandate` / portfolio proof.
- **Best execution / MEV-arbitrage recapture (3)** — solver auction, integer-exact surplus
  split, `ProofOfBestExecution` / `ProofOfSurplusRecapture`, the `settle_via_auction` bridge.
- **x402 Phase 1** — an x402-shaped facilitator (verify/settle, mandate-gated) + a live HTTP
  402 demo.
- **x402 Phase 2** — a real x402 **v1** client (`exact`/EVM, EIP-3009 + EIP-712) with a
  security-hardened `RemoteFacilitatorAdapter`, tested against stubs.
- **x402 Phase 3** — the `best-exec` scheme (③ as an x402 scheme): mandate-gated solver
  auction + integer-exact surplus recapture, nonce-bound to a fixed-value EIP-3009
  authorization, with a written spec ([`specs/best-exec.md`](specs/best-exec.md)) and offline
  third-party re-verification. Offline accounting layer; the audited on-chain settler is out
  of core (§ hard gates).

223 tests, 7 examples, determinism + import-discipline guards, zero third-party runtime deps
(EVM signing isolated behind the optional `[evm]` extra).

## Release track — take it public "the x402 way"

- **R1 — Open-source the repository (Apache-2.0).** Public GitHub with README, docs,
  examples, spec, and CI. *(You: create the empty repo + grant the Claude GitHub app access;
  then I push. Publishing is your call — it distributes the project under your name.)*
- **R2 — Protocol spec (`specs/`).** A written spec for the mandate model and the x402
  integration, in the style of x402's `specs/`. The `best-exec` scheme is specified in
  [`specs/best-exec.md`](specs/best-exec.md); a mandate-model spec is still to write.
- **R3 — Community + release hygiene.** `CONTRIBUTING`, `SECURITY`, `CODE_OF_CONDUCT`,
  `ROADMAP`, issue/PR templates, and a signed-release + PyPI trusted-publishing workflow.
- **R4 — Publish to PyPI.** `pip install mandatehub` (+ `mandatehub[evm]`). *(You own the PyPI
  project + configure the trusted publisher pointing at the repo; the workflow does the rest.)*
- **R5 — Docs site + reference deployment.** A docs site and landing page on Cloudflare Pages
  under your domain, plus a hosted **testnet** reference facilitator / resource-server on
  Cloudflare Workers — aligning with x402's edge deployment (x402 Foundation, Cloudflare
  Agents). The build + config are prepared: a self-contained static landing ([`site/`](site/)),
  a Python Worker that reuses `serve_once` ([`deploy/cloudflare/`](deploy/cloudflare/)), and the
  runbook ([`docs/DEPLOY_CLOUDFLARE.md`](docs/DEPLOY_CLOUDFLARE.md)). Durable-ledger (D1) is
  gate H2. *(You: Cloudflare account + domain; I: the build + config.)*

## Protocol track — mature the payment path (testnet first)

- **P3 — `best-exec` x402 scheme (built; offline accounting layer).** Exposes (3) as an x402
  scheme: the facilitator best-executes within the user's max and rebates the surplus, with
  both proofs in the response and offline third-party re-verification. Spec:
  [`specs/best-exec.md`](specs/best-exec.md). The audited on-chain `BestExecSettler` contract
  (atomic split, nonce binding, no rebate withholding) is **out of core and unbuilt** — gate
  H1. Design → implement → adversarial review: done.
- **P-live — Testnet validation.** Run `examples/x402_live_smoke.py` against a real facilitator
  on **Base Sepolia**, then a full `402 → pay → settle → proof` loop on testnet.

## Hard gates before mainnet / real money

Publishing the open-source project is safe. **Moving real value is not, until:**

- **H1 — Independent security review / audit** of the payment, settlement, and key-handling
  paths (the adversarial-review passes so far are internal, not a substitute for an audit).
- **H2 — Production hardening** — durable storage (Postgres via the `LedgerStorage` protocol),
  key management / KMS, auth, rate limiting, monitoring, incident runbooks, fail-closed on
  settlement saturation.
- **H3 — Legal / compliance review** — moving stablecoin value has regulatory implications;
  get counsel before mainnet. This is not legal advice.

Only after H1–H3: mainnet, real USDC.

## Honest status

mandatehub is an **independent, early, unproven** project — not backed by any foundation or
institution, and with no production adoption. Nothing here is legal or financial advice. The
release track is publishing open-source software; the protocol/mainnet track is gated on the
real-world reviews above.
