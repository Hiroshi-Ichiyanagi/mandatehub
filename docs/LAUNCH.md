# Launch kit — mandatehub

Ready-to-post materials for announcing the machine-payable service. **Every number here must
stay faithful** (see the project's honest-numbers discipline): today mandatehub is a live,
self-funded pilot — real USDC settles on Base mainnet, but the only buyer so far is our own
agent. Do not imply external traction we don't have.

Refresh the numbers before posting:

```bash
curl -s https://mandatehub.obolpay.xyz/metrics | python3 -m json.tool
```

As of 2026-07-22: **14 settlements, 0.14 USDC gross, 1 unique payer (self), 0 external buyers.**

---

## One-liner

> **mandatehub** — AI agents pay ~$0.01 in USDC per call for verifiable data & proofs, over
> x402 (HTTP 402 on Base). Budget-capped, replay-proof, every response carries an on-chain
> settlement tx and an offline-checkable ProofOfMandate.

## Elevator paragraph

mandatehub is an x402 resource server with a **mandate gate**: an agent may only spend inside a
pre-funded, budget-capped authorization, can't replay or exceed it, and gets back canonically-
hashed JSON plus an on-chain settlement tx and a `ProofOfMandate` anyone can verify offline.
On top of it we sell eight machine-readable products (FX reference rates, zero-spread FX
conversion, LLM backend-selection matrices, audit-log verification, on-chain tx verification,
evidence-bundle verification, a population-weighted unit of account, and a JP-equity
convergence snapshot) for 0.01 USDC each. Rejected payments (replay / over-budget / rate) cost
nothing — they never touch the chain.

## Links

- Live service: <https://mandatehub.obolpay.xyz> (try `/quote`, `/.well-known/agents.json`)
- Library: `pip install mandatehub` — <https://pypi.org/project/mandatehub/>
- Source: <https://github.com/Hiroshi-Ichiyanagi/mandatehub>
- Colab (free, no wallet): [quickstart notebook](https://colab.research.google.com/github/Hiroshi-Ichiyanagi/mandatehub/blob/main/examples/mandatehub_quickstart.ipynb)

---

## X / Farcaster post (draft)

> Agents can now buy verifiable data for ~$0.01 USDC/call.
>
> mandatehub is an x402 service on Base with a mandate gate: budget-capped, replay-proof, and
> every response ships an on-chain settlement tx + an offline-checkable ProofOfMandate.
>
> Discover it the way an agent does:
> `curl https://mandatehub.obolpay.xyz/.well-known/agents.json`
>
> MCP server + LangChain/CrewAI tools included. Free Colab to try it without a wallet 👇

(Thread 2/) 8 products live — FX rates, zero-spread FX, LLM backend matrices, audit-log &
on-chain-tx & evidence-bundle verification, a population-weighted unit of account, JP-equity
scores. Rejected payments cost nothing; they never touch the chain.

(Thread 3/) Honest status: self-funded pilot, pre-audit. Real USDC, real settlement, but the
only buyer so far is our own agent. Kicking the tires welcome — the quote endpoint is free to
preview. Source + roadmap in the repo.

## Coinbase x402 Bazaar / CDP submission blurb

> mandatehub exposes eight pay-per-call products over the x402 `exact` scheme on Base mainnet
> (USDC), each at a stable per-resource URL. Requests are gated by a budget-capped mandate:
> replay-, over-budget-, and rate-violations are rejected before settlement (zero on-chain
> cost). Every paid response returns the product JSON, the settlement tx, an independent chain
> verification, and a `ProofOfMandate`. Machine discovery: `/.well-known/agents.json`,
> `/.well-known/ai-plugin.json`, `/openapi.json`. Eligibility (`validate`) accepted; requesting
> indexing.

## Hackathon / demo pitch (60s)

1. `curl .../.well-known/agents.json` → the catalog an agent reads.
2. `curl .../quote` → HTTP 402 with the price; nothing charged.
3. `python examples/x402_pay.py .../quote` → real USDC settles, data + settlement tx +
   ProofOfMandate come back.
4. Show a rejected replay: same intent twice → second is refused, no chain call.

The hook: **the gate, not just the payment** — an agent that provably cannot overspend.

---

## Guardrails (keep it honest)

- Say "self-funded pilot / pre-audit". Don't call it a business or imply external revenue.
- "0 external buyers" until `unique_payees > 1` in `/metrics`. Re-check before every post.
- Bazaar listing is **pending CDP's crawl** — "requesting indexing", not "listed".
- Hard gates before scaling others' money: H1 security audit, H2 shared-store/KMS, H3 legal
  ([ROADMAP](../ROADMAP.md)). Don't promise around them.
