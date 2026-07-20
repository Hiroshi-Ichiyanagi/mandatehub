# Cloudflare Workers — mandatehub testnet reference (R5, Part B)

A **Python Worker** that serves the x402 `402 Payment Required` handshake gated by a mandate,
reusing `mandatehub.x402.serve_once` unchanged. This is the **testnet demo** tier — no mainnet
value; a durable ledger (Cloudflare D1) is gate **H2**. Full design in
[`../../docs/DEPLOY_CLOUDFLARE.md`](../../docs/DEPLOY_CLOUDFLARE.md).

## Files

- `wrangler.toml` — Worker config (`python_workers`, `main = worker.py`), demo vars, and the
  (commented) D1 binding a durable deployment would add.
- `worker.py` — the `on_fetch` handler: builds `PaymentRequirements`, calls `serve_once`, and
  maps `(status, body, headers)` back to a `Response`.

## Deploy (owner)

```bash
npm i -g wrangler
wrangler login                       # your Cloudflare account
cd deploy/cloudflare
wrangler dev                         # run on the local edge runtime
wrangler deploy                      # publish
```

For real testnet `/settle`, add secrets (never commit them):

```bash
wrangler secret put MANDATEHUB_FACILITATOR_URL     # a Base Sepolia x402 facilitator
wrangler secret put MANDATEHUB_AGENT_PRIVATE_KEY   # throwaway testnet key
```

Without secrets the Worker still serves the `402` handshake and the in-ledger (mock) settle for
a demo.

## Verification status (honest)

- The **mandatehub logic** in `worker.py` (facilitator build, `PaymentRequirements`,
  `serve_once` → 402 with `PAYMENT-REQUIRED`) is **verified locally**: the same calls run
  end-to-end against the library and produce the 402 handshake.
- The **Workers glue** (`from workers import Response`, `on_fetch`) has **not** been executed on
  the Cloudflare runtime in this repo — `wrangler dev` is the confirmation step. Python Workers
  run on Pyodide; confirm `mandatehub` (stdlib-only, incl. in-memory `sqlite3`) loads there on
  first `dev`.
- State is **per-isolate in-memory** (demo). A durable, replay-safe ledger binds the
  `LedgerStorage` protocol to D1 / Durable Objects — that is gate **H2**, not this demo.
