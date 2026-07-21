# Testnet validation (P-live — Base Sepolia)

The **P-live** roadmap step: confirm mandatehub's real x402 **v1** client (the `exact`/EVM
scheme, Phase 2) is accepted by a real facilitator on **Base Sepolia**, then run a full
`402 → pay → settle → proof` loop on testnet. This moves **testnet** value only — never
mainnet, never real money (that is gated on H1–H3 in [ROADMAP](../ROADMAP.md)).

Per [OPERATIONS.md](OPERATIONS.md), anything that submits a transaction is an owner action:
you supply the facilitator, the key, and the testnet funds; the code and this runbook are the
prepared, verified half.

> **P-live COMPLETED (2026-07-21).** The full flow ran against the real
> `https://x402.org/facilitator` on Base Sepolia with a real key:
>
> 1. *Wire format* — a no-key `StubSigner` payload returned
>    `invalid_exact_evm_signature` (v1 parsed end-to-end to signature verification).
> 2. *`/verify`* — with a real key + funded wallet: **`isValid=true`**.
> 3. *`/settle`* — **`success=true`**, on-chain USDC transfer
>    ([tx `0x4b6c…c901`](https://sepolia.basescan.org/tx/0x4b6c4bf9c68124867f7ddc8cd0bd305a6a88a20bafd1c3b6e58cabdb1deac901),
>    0.01 USDC payer→recipient, both balance changes confirmed via RPC). The payer wallet
>    held **zero ETH** throughout — the facilitator paid gas, confirming the EIP-3009
>    gasless design in practice.
>
> This runbook remains the reproducible path for re-running the validation.

## The three owner inputs (the gate)

| input | what | how |
| ----- | ---- | --- |
| a facilitator | a real x402 v1 facilitator URL | `https://x402.org/facilitator`, or a Coinbase CDP facilitator (needs auth headers — see below) |
| an agent key | a Base Sepolia EVM private key | a throwaway dev wallet; **never** a mainnet key |
| testnet USDC | Base Sepolia USDC in that wallet | Circle faucet <https://faucet.circle.com> (select Base Sepolia) + a Base Sepolia ETH faucet for gas |

Base Sepolia constants are built in and correct (chain id `84532`, USDC
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`, EIP-712 domain `{name:"USDC", version:"2"}`; the
domain separator was recomputed on-chain to a byte-exact match — see
[X402.md § Phase 2](X402.md#phase-2-real-x402-v1-client)).

## Step 0 — Preflight (offline; no network, no key, no funds)

Catch every config mistake *before* spending a real call. `examples/x402_live_preflight.py`
validates the whole client wiring offline with a `StubSigner`:

```bash
export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator
export MANDATEHUB_PAY_TO=0xYourRecipient
python examples/x402_live_preflight.py           # exit 0 = ready
```

It checks the required env is set, the URL passes the `https` guard, and the exact/EVM payload
builds + base64-encodes. It never touches the network or needs USDC. *(Verified 2026-07-21:
exit 0 on a well-formed config; exits non-zero with a clear message on missing env or a
non-https URL.)* Add `MANDATEHUB_AGENT_PRIVATE_KEY` (with `pip install 'mandatehub[evm]'`) to
also preflight the real signer's address derivation — still offline.

## Step 1 — `/verify` only (non-destructive)

```bash
pip install 'mandatehub[evm]'
export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator
export MANDATEHUB_AGENT_PRIVATE_KEY=0x...         # Base Sepolia agent key (throwaway)
export MANDATEHUB_PAY_TO=0x...                    # recipient
python examples/x402_live_smoke.py               # calls /verify, moves nothing
```

Reading the result:

- `isValid=true` — the facilitator fully accepts our v1 payload. 
- `isValid=false, invalidReason="insufficient_funds"` — **also a success for P-live's purpose**:
  the facilitator parsed our v1 wire format and answered in v1. Just fund the agent wallet with
  Base Sepolia USDC and re-run.
- a `FacilitatorError` (HTTP / malformed JSON / network) — a wiring or auth problem, not a
  funds problem; see § Troubleshooting.

## Step 2 — `/settle` (opt-in; moves real testnet value)

Only after Step 1 confirms the wire format and the wallet is funded:

```bash
MANDATEHUB_LIVE_SETTLE=1 python examples/x402_live_smoke.py
# /settle -> success=…  transaction=0x…  errorReason=…
```

`success=true` with a `transaction` hash is the on-chain testnet settlement — verify it on the
Base Sepolia explorer (<https://sepolia.basescan.org>).

## Step 3 — the full `402 → pay → settle → proof` loop ✅ (ran live 2026-07-21)

**Completed live**: `examples/x402_live_loop.py` ran the whole loop against the real
`x402.org` facilitator on Base Sepolia — a localhost resource server speaking the real v1
`X-PAYMENT` wire, with the mandate gate (budget cap 0.025 USDC) in front of settlement:

| call | result |
| ---- | ------ |
| fresh payment | `200` + on-chain settle ([tx `0x3586…4c25`](https://sepolia.basescan.org/tx/0x35869b2e27a158b452f4dd10a84c8e0250bdf6cb1d27a425987b67305fe94c25)) + `ProofOfMandate` |
| **replayed** `X-PAYMENT` | `402 DUPLICATE_INTENT` — mandate blocked it **before** the facilitator (no network call, no chain action) |
| fresh payment | `200` + on-chain settle ([tx `0xb6c9…d2f8`](https://sepolia.basescan.org/tx/0xb6c9ee460fde90f9bcda0bdba3cd4181ff1861e89d43138eef4bbcb1e69ed2f8)) |
| over-budget payment | `402 BUDGET_EXCEEDED` — denied for free, fail-closed |

Both payer/merchant balance deltas were confirmed via RPC. Run it yourself with the same
env as the smoke test: `python examples/x402_live_loop.py`.

Compose the mandate gate around the real settlement: a resource server returns `402` with
`PaymentRequirements`; the agent builds the `exact` payload (Step 1); the facilitator settles
on testnet (Step 2); and mandatehub rides a `ProofOfMandate` back in the `PAYMENT-RESPONSE`.
The offline HTTP shape of this loop already runs end-to-end in
[`examples/x402_facilitator.py`](../examples/x402_facilitator.py) (ledger adapter); P-live
swaps its `SettlementAdapter` for the `RemoteFacilitatorAdapter` pointed at Base Sepolia. The
mandate-gate mitigations that wrap the adapter (server-derived requirements, atomic
nonce check-and-lock, deliver-only-after-`success:true`, fail-closed on `429`) are described in
[X402.md § Still gated before mainnet](X402.md#still-gated-before-mainnet).

## Coinbase CDP facilitator ✅ (confirmed live 2026-07-21)

The official CDP facilitator — the same family that serves **mainnet** — is fully integrated
and was confirmed live end-to-end: `/verify` → `isValid=true` and `/settle` → an on-chain
Base Sepolia transfer, both through the packaged helper:

```python
from mandatehub.signers import CDP_FACILITATOR_URL, cdp_header_hook_from_file
adapter = RemoteFacilitatorAdapter(CDP_FACILITATOR_URL, network="base-sepolia",
                                   header_hook=cdp_header_hook_from_file())  # ~/.mandatehub-cdp.json
```

Requires `pip install 'mandatehub[cdp]'` (the official `cdp-sdk` builds the per-request
Ed25519 JWT bound to `POST {host}{path}`). Confirmed CDP specifics, learned live:

- v1 `paymentRequirements` **must include `description` and `mimeType`** (CDP validates
  strictly; x402.org does not require them).
- An invalid payment returns **HTTP 400 with the regular verify/settle JSON body** (x402.org
  returns 200 + `isValid=false`); `RemoteFacilitatorAdapter` handles both shapes.
- **Self-send is rejected** (`self_send_not_allowed`): `payTo` must differ from the payer.
- The key file `~/.mandatehub-cdp.json` is `{"keyId": ..., "keySecret": ...}` (chmod 600);
  the secret never appears in headers, logs, or errors.

The `x402.org` facilitator needs no auth. For **mainnet** the same code path applies with
`network="base"`, `BASE_MAINNET_USDC`, and `extra=BASE_MAINNET_USDC_DOMAIN` (the domain name
is `"USD Coin"`, not `"USDC"` — constants on-chain-verified) — gated on H1–H3.

## Env reference

| env var | default | purpose |
| ------- | ------- | ------- |
| `MANDATEHUB_FACILITATOR_URL` | — (required) | facilitator base URL; `https://` only |
| `MANDATEHUB_PAY_TO` | — (required) | recipient address |
| `MANDATEHUB_AGENT_PRIVATE_KEY` | — (smoke test) | Base Sepolia signer key; consumed only by `EthAccountSigner`, never logged |
| `MANDATEHUB_AMOUNT` | `10000` | amount in minor units (0.01 USDC at 6 decimals) |
| `MANDATEHUB_NETWORK` | `base-sepolia` | network slug |
| `MANDATEHUB_ASSET` | Base Sepolia USDC | token address |
| `MANDATEHUB_LIVE_SETTLE` | unset | `1` opts into `/settle` (real testnet value) |

## Troubleshooting

- **`missing required env var`** — run Step 0 preflight; it names the missing var.
- **`facilitator URL must be https`** — the adapter refuses non-https (localhost excepted for
  local tests); use an `https://` facilitator.
- **`insufficient_funds`** — expected until the agent wallet holds Base Sepolia USDC; fund it.
- **`FacilitatorError: HTTP 403`** — usually the facilitator's WAF (Cloudflare bot fight,
  `error code: 1010`) rejecting a default `Python-urllib` User-Agent. The client now sends a
  `mandatehub-x402/1` UA by default, so this should not recur; a `header_hook` can override
  the UA if a facilitator requires something specific. (Observed live against `x402.org`.)
- **`FacilitatorError: HTTP 401`** — the facilitator needs auth headers (CDP); pass a
  `header_hook`.
- **`only x402 v1 is supported`** — the payload/response declared a non-v1 `x402Version`; this
  build speaks v1 only.

## Safety

- Use a **throwaway** Base Sepolia wallet. Never point `MANDATEHUB_AGENT_PRIVATE_KEY` at a key
  that holds mainnet value.
- `/settle` is behind an explicit `MANDATEHUB_LIVE_SETTLE=1` opt-in and moves testnet value
  only. Mainnet is gated on H1–H3.
