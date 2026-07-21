# Announcing mandatehub — ready-to-adapt copy

Draft launch copy for the operator to publish (edit to taste; keep the honesty).

---

## Short (X / social)

> **mandatehub** is live. 🟢
>
> An x402 payment layer where an autonomous agent's spending is **provably bounded,
> replay-proof, and proof-carrying** — settling real USDC on Base.
>
> `pip install mandatehub` · pay a live endpoint:
> `python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote`
>
> Open source (Apache-2.0). Early & unproven — self-funded pilot, not audited.
> → github.com/Hiroshi-Ichiyanagi/mandatehub

---

## Medium (README-style blurb / HN / dev forum)

**mandatehub** — provable autonomous machine-to-machine payment.

It's the mandate + proof layer inside an [x402](https://github.com/coinbase/x402) facilitator:
a **mandate** is a pre-funded, budget-bounded authorization; an autonomous agent settles
intents within it; and a `ProofOfMandate` lets anyone verify **offline** that the budget was
never exceeded, no payment was replayed, and any best-execution surplus was recaptured
honestly. Standard library only, zero runtime deps.

What's real today:

- **Library**: `pip install mandatehub` (`[evm]` for signing) — 244 tests, CI on 3.11–3.13.
- **Live service**: <https://mandatehub.obolpay.xyz> — a running x402 resource server with a
  mandate gate, settling real USDC on Base via the Coinbase CDP facilitator. `/`, `/healthz`,
  `/metrics`, `/quote`.
- **Consume it**: any x402 client, or `python examples/x402_pay.py <url>`.
- **Proven end-to-end**: `402 → pay → on-chain settle → ProofOfMandate`, with replayed and
  over-budget payments denied *before* any settlement (fail-closed, free).

Honest status: **early and unproven**, no production adoption, self-funded pilot. Moving real
value at scale is gated on an independent audit, production hardening, and legal review — see
the [roadmap](https://github.com/Hiroshi-Ichiyanagi/mandatehub/blob/main/ROADMAP.md). Nothing
here is financial or legal advice.

---

## For developers (how to use it in 60 seconds)

**Charge for your API in USDC (be a seller):**
```bash
git clone https://github.com/Hiroshi-Ichiyanagi/mandatehub && cd mandatehub
pip install -e '.[evm]'
export MANDATEHUB_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
export MANDATEHUB_NETWORK=base MANDATEHUB_PAY_TO=0xYourWallet
export MANDATEHUB_CDP_KEY_FILE=~/.mandatehub-cdp.json
python deploy/local/operator.py          # your mandate-gated x402 endpoint on :8403
```

**Pay an x402 endpoint (be a buyer):**
```bash
export MANDATEHUB_AGENT_PRIVATE_KEY=0x...  # a Base-funded key
python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote
# → 200 + data + on-chain settlement tx + ProofOfMandate
```

Docs: [operating at scale](docs/OPERATING_AT_SCALE.md) ·
[testnet runbook](docs/TESTNET.md) · [mandate spec](specs/mandate.md).
