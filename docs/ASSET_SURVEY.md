# Asset survey — productization verdict (2026-07-22)

A full pass over ~40 of Hiroshi's projects (`~/dev`, `~/Downloads`) for machine-payable
products, with the earlier "keep it light" constraint removed. What shipped, what's queued,
and what is honestly not a machine-buyer product.

## Shipped as live products (7, all on the VPS)

| product | source asset | note |
| ------- | ------------ | ---- |
| `fx` | genesis_finance (idea) | zero-spread cross-currency conversion over ECB rates |
| `qswap` | qswap | measured backend fidelity/swap matrices (vendored JSON) |
| `audit-verify` | genesis-keystone | verify a caller's hash-chained audit log vs its signed anchor |
| `verify-tx` | mandatehub | on-chain Base USDC transfer verification |
| `govern-verify` | govern-open-verify (pyverify) | pure-Python bundle verifier; demo + caller-zip |
| `openunit` | openunit | population-weighted unit-of-account valuation, re-verified live |
| `kairos` | kairos | JP-equity convergence scores (static snapshot, honest as-of) |
| `/quote` | x402-gateway (idea) | the ECB feed, kept as the default endpoint |

## Queued (real machine-buyer value; needs more integration)

- **chorus** — verifiable multi-model (Mixture-of-Agents) amplification with proofs. A
  strong "run a verified multi-model answer, pay per call" product, but it orchestrates
  multiple LLMs (cost + latency + provider keys). Fits a higher price tier; deferred.
- **chord_oracle** — audio → chord-progression extraction vs a reference tab. Genuine
  machine product (submit audio, get JSON), but needs audio deps + POST upload of media.
- **openreserve** (= verification-core, already on PyPI) — its ledger/Merkle/audit
  primitives overlap mandatehub's own; more a library than a distinct endpoint.
- **tessera / tritie / spendcap** — verifiable-inference / binding / spend-capsule cores;
  largely consolidated into `govern`, which `govern-verify` already covers.

## Deferred by policy (sensitivity), not capability

- **ibkr_quant**, **genesis-securities-sim**, **compound**, **accrue** — trading / brokerage
  / revenue engines. Real signal value, but selling trading signals raises advice/regulatory
  questions (mandatehub's own H3 stance) — keep out of the public catalog until the owner
  decides. `kairos` ships only because it's clearly labeled a static research snapshot, not
  advice or live data.

## Honestly not machine-buyer products

- **genesis_tutor** (children's learning companion), **genesis-studio / GENESISAcoustic /
  AURALIS** (music/DTM/DSP for humans), **yorishiro** (a persona adapter — infrastructure,
  not a per-call good), **genesis_conduit** (an MCP transport), **prism** (an AI
  architecture), **sigil** (archived into govern), **genesis_gl** (design-phase). These are
  human-facing apps or infrastructure with no clean per-call JSON a machine would buy.

## Principle

A product ships only if a *machine* would pay $0.01 for its JSON, it degrades fail-closed
(availability gate → 503, never charge for what can't be served), and it can be served or
verified honestly (live re-verification, explicit as-of, or deterministic hash).
