# x402 Bazaar listing (Coinbase CDP discovery)

How agents discover mandatehub's live endpoint, what CDP requires to list it, and the exact
gap — determined empirically against the CDP API (not guessed).

## What we found (2026-07-21)

- The Bazaar is **read-only via API**: `GET /platform/v2/x402/discovery/resources` lists
  resources; there is **no public registration POST** (every candidate returned 404).
- Listing is **not automatic** from settling through the CDP facilitator: our mainnet
  settlements go through CDP, yet `GET …/discovery/merchant?payTo=<ours>` returns
  `total: 0` and our resource is absent from the 100-item list.
- The registration mechanism is **`POST /platform/v2/x402/validate`** (`validate_x402_resource`
  in `cdp-sdk`): it **probes the live endpoint** and reports what's needed. Running it against
  `https://mandatehub.obolpay.xyz/quote` returned:

  | check | result |
  | ----- | ------ |
  | `url_valid`, `url_https`, `endpoint_reachable` (402), `valid_json` | ✅ PASS |
  | `x402_version` | ❌ **"Endpoint uses x402 v1 — upgrade to x402 v2 to be discoverable in the bazaar"** |
  | everything downstream (`has_accepts`, `has_bazaar_extension`, `bazaar.info…`, `schema`) | skipped (blocked on v1) |
  | `simulation.outcome` | `rejected` — "upgrade to x402 v2 for bazaar discovery" |

**So the one gate to Bazaar discoverability is: serve an x402 v2 `402` with a `bazaar`
extension.** Our payment path already works on v1 through CDP; discovery is a separate concern
about the *shape of the 402 challenge*.

## The v2 shape CDP wants (reverse-engineered from a listed resource)

A listed v2 resource's `402` body carries:

```jsonc
{
  "x402Version": 2,
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:8453",           // CAIP-2 (v1 used the slug "base")
    "amount": "10000",                  // v2 uses "amount" (v1 used "maxAmountRequired")
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x…",
    "maxTimeoutSeconds": 300,
    "extra": { "name": "USD Coin", "version": "2" }
  }],
  "description": "…human + machine description…",
  "extensions": {
    "bazaar": {
      "routeTemplate": "/quote",
      "info": {
        "input":  { "type": "http", "method": "GET" },
        "output": { "type": "json", "example": { /* a sample 200 body */ } }
      },
      "schema": { "$schema": "https://json-schema.org/…", /* output schema */ }
    }
  }
}
```

`validate` requires `has_bazaar_extension`, `bazaar.info.input.{type,method}`, `bazaar.schema`
(all required) and `bazaar.info.output.example` (advisory).

## x402 v2 — DONE (validated by CDP 2026-07-21)

The operator now serves an **x402 v2** discovery challenge at `/quote-v2`
(`https://mandatehub.obolpay.xyz/quote-v2`), and CDP's `validate` accepts it:

```
VALID: True   simulation: {"outcome": "accepted"}   (all required preflight checks pass)
```

What v2 required (learned via the validate loop):
1. The PaymentRequired payload delivered in a **`PAYMENT-REQUIRED` response header**
   (base64 JSON) — "the indexer reads only the header for v2".
2. **CAIP-2** network (`eip155:8453`) and `amount` (not `maxAmountRequired`).
3. A top-level **`resource: {url}`** object.
4. `extensions.bazaar` with `info.input`/`info.output.example` and a **`schema` that describes
   the `info` object** (properties `input`/`output`) — not the response body.

`deploy/local/validate_bazaar.py <url>` re-runs this check (the listing acceptance test).
`/quote-v2` also forwards real payments to the CDP facilitator (same money-path as `/quote`).

## Remaining (indexing)

`validate` is a *checker*, not an *indexer* (it returns `index: null`). Listing in the public
Bazaar follows separately — CDP indexes eligible resources (a crawl, and/or resources with
settlement activity through its facilitator). The endpoint is now **eligible** (validated);
re-check with `discovery/merchant?payTo=<ours>` until it appears.

## (superseded) original scoped step

Flipping discoverability on is an **x402 v1 → v2 upgrade of the operator's challenge** (and,
for agents to pay it, v2 acceptance): change the `402` body to the v2 shape above + the
`bazaar` extension, keep CDP settlement working. Because it changes the live money-path
protocol version, it belongs in its own careful PR (OPERATIONS: incremental on the money path),
with the `validate` API as the acceptance test — re-run it until every required check passes,
then the resource auto-lists. The ready-to-serve `bazaar` metadata for our `/quote` endpoint is
in the operator (`_bazaar_extension`), so the upgrade is mostly wiring the v2 envelope.

## Owner note

No CDP-portal click is needed — `validate` is the registration path, callable with the CDP API
key (`cdp_header_hook`). Once the endpoint passes `validate`, it becomes discoverable; re-run:

```python
# POST https://api.cdp.coinbase.com/platform/v2/x402/validate
# body: {"resource": "https://mandatehub.obolpay.xyz/quote", "method": "GET"}
```
