# Examples

Runnable scripts using only the public `mandatehub` API. Each is self-contained and uses
in-memory storage, so they leave nothing behind.

```bash
python examples/intent_mandate_settlement.py   # budget-bounded autonomous settlement + proof
python examples/best_execution_recapture.py     # solver auction + surplus recapture (3<->4 bridge)
python examples/session_key_submandate.py       # session-key sub-mandates with provable non-leakage
python examples/x402_facilitator.py             # live HTTP 402 flow gated by a mandate (x402-compatible)
python examples/x402_remote_settle.py           # real x402 v1 exact/EVM payload -> facilitator /verify + /settle (stub)
```

| Script | Shows |
| ------ | ----- |
| `intent_mandate_settlement.py` | Grant a pre-funded mandate (budget, allowed purposes, window, per-tx cap), settle M2M intents autonomously with every accept/deny on a tamper-evident audit chain, then produce a `ProofOfMandate` — and let a payee verify its own receipts' inclusion. |
| `best_execution_recapture.py` | Fill an intent through a deterministic solver auction, split the price-improvement surplus (user rebate / operator margin / gas) in one balanced transaction, and emit a best-execution proof + a surplus-recapture proof. Shows the two value planes (INV-9). |
| `session_key_submandate.py` | Delegate bounded sub-budgets to two session keys drawing on one root mandate; every ancestor's budget is re-derived from the ledger, so combined descendant spend can never exceed the parent. |
| `x402_facilitator.py` | Run a real localhost HTTP server that charges via the x402 `402 Payment Required` handshake, gated by a mandate: `402 → pay → 200 + ProofOfMandate`, and a replayed payment is rejected by the mandate over HTTP. |
| `x402_remote_settle.py` | Build a real x402 **v1** `exact`/EVM payment payload (EIP-3009 + EIP-712, signed by a `StubSigner`) and call a facilitator's `/verify` and `/settle` over HTTP via `RemoteFacilitatorAdapter` — against an in-process stub, so it runs with no network and no keys. Shows the exact wire the live CDP facilitator speaks. |
| `x402_pay.py` | **(network; consumer side)** Pay ANY x402 `exact` endpoint from the CLI: read its 402 terms, sign an EIP-3009/EIP-712 payment, resend, print the resource + `ProofOfMandate`. `--quote-only` inspects the terms without paying. Same client pays testnet or mainnet (network comes from the 402). Try it live: `python examples/x402_pay.py --quote-only https://mandatehub.obolpay.xyz/quote`. |
| `mcp_server.py` | **(network; consumer side)** An MCP server exposing the live service as native agent tools — `discover()` / `openapi()` / `preview(path)` (free) and `purchase(path)` (spends real USDC). Reuses the library's exact x402 payload builder, so it can't drift from `x402_pay.py`. `pip install "mcp[cli]" "mandatehub[evm]"`; add it to Claude Desktop / Cursor. See the "Agent integration" section of the top-level README. |
| `x402_live_preflight.py` | **(offline; no network, no key, no funds)** Validate the live-smoke client wiring before spending a real call: required env set, facilitator URL passes the `https` guard, and the exact/EVM payload builds + encodes (with a `StubSigner`). Run this first; exit 0 = ready. |
| `x402_live_smoke.py` | **(network; not in the offline suite)** Sign a real payload with `EthAccountSigner` (`pip install 'mandatehub[evm]'`) and hit a **real** facilitator's `/verify` (opt-in `/settle`) using your own env-supplied facilitator URL + agent key. Confirms the v1 assumptions against Base Sepolia. See [../docs/TESTNET.md](../docs/TESTNET.md) for the full P-live runbook. |

See [../docs/INTENT_MANDATES.md](../docs/INTENT_MANDATES.md) and
[../docs/EXECUTION_RECAPTURE.md](../docs/EXECUTION_RECAPTURE.md) for the model these demonstrate.
