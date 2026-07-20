# Examples

Runnable scripts using only the public `mandatehub` API. Each is self-contained and uses
in-memory storage, so they leave nothing behind.

```bash
python examples/intent_mandate_settlement.py   # budget-bounded autonomous settlement + proof
python examples/best_execution_recapture.py     # solver auction + surplus recapture (3<->4 bridge)
python examples/session_key_submandate.py       # session-key sub-mandates with provable non-leakage
python examples/x402_facilitator.py             # live HTTP 402 flow gated by a mandate (x402-compatible)
```

| Script | Shows |
| ------ | ----- |
| `intent_mandate_settlement.py` | Grant a pre-funded mandate (budget, allowed purposes, window, per-tx cap), settle M2M intents autonomously with every accept/deny on a tamper-evident audit chain, then produce a `ProofOfMandate` — and let a payee verify its own receipts' inclusion. |
| `best_execution_recapture.py` | Fill an intent through a deterministic solver auction, split the price-improvement surplus (user rebate / operator margin / gas) in one balanced transaction, and emit a best-execution proof + a surplus-recapture proof. Shows the two value planes (INV-9). |
| `session_key_submandate.py` | Delegate bounded sub-budgets to two session keys drawing on one root mandate; every ancestor's budget is re-derived from the ledger, so combined descendant spend can never exceed the parent. |
| `x402_facilitator.py` | Run a real localhost HTTP server that charges via the x402 `402 Payment Required` handshake, gated by a mandate: `402 → pay → 200 + ProofOfMandate`, and a replayed payment is rejected by the mandate over HTTP. |

See [../docs/INTENT_MANDATES.md](../docs/INTENT_MANDATES.md) and
[../docs/EXECUTION_RECAPTURE.md](../docs/EXECUTION_RECAPTURE.md) for the model these demonstrate.
