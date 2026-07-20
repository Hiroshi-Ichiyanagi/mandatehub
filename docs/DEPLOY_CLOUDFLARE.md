# Deploying on Cloudflare (R5 — docs site + testnet reference)

The **R5** roadmap step: a **docs site + landing page** on Cloudflare Pages under your domain,
and a hosted **testnet** reference **facilitator / resource-server** on Cloudflare Workers —
mirroring how x402 ships an edge deployment. This aligns with
[OPERATIONS.md](OPERATIONS.md): the deployment is a **separate, owner-owned artifact** (account,
domain, secrets) from this public library. The build + config below are the prepared half; the
account, domain, and any keys are the owner gate.

Nothing here moves mainnet value. The Worker settles on **Base Sepolia** (testnet) only, and
mainnet stays gated on H1–H3.

## Two artifacts

| | Cloudflare Pages (docs + landing) | Cloudflare Workers (testnet reference) |
| --- | --- | --- |
| what | static site: landing + rendered docs | a resource-server that speaks HTTP `402`, mandate-gated |
| source | [`site/`](../site/) (static, no build step) | [`deploy/cloudflare/`](../deploy/cloudflare/) (`wrangler.toml` + `worker.py`) |
| moves value | no | testnet only (delegates settlement to a Base Sepolia facilitator) |
| owner inputs | Cloudflare account + domain | account + (for `/settle`) a facilitator URL + testnet key via secrets |

## Part A — docs site + landing (Cloudflare Pages)

[`site/`](../site/) is a **self-contained static site** — a single `index.html` with inline
styles, no build step, no dependencies. Cloudflare Pages can serve the directory as-is.

Owner runbook:

1. **Create the Pages project.** Cloudflare dashboard → *Workers & Pages → Create → Pages →
   Connect to Git* → select `Hiroshi-Ichiyanagi/mandatehub`. Set **Build output directory** to
   `site` and leave the build command empty (static). Or, without Git: `wrangler pages deploy
   site` from a checkout.
2. **Custom domain.** Pages project → *Custom domains* → add your domain/subdomain (e.g.
   `mandatehub.<yourdomain>`); Cloudflare provisions TLS.
3. **Verify.** The landing renders the project summary and links to the repo, the specs
   (`mandate`, `best-exec`), and the docs. `site/_headers` sets safe defaults
   (`X-Content-Type-Options`, a strict `Content-Security-Policy` — the page uses no external
   scripts).

To grow it into full rendered docs later, point a static-site generator (MkDocs Material, or
Cloudflare's own) at the existing `docs/` Markdown and set that tool's output as the Pages
build directory — the `site/` landing stays the entry point.

## Part B — testnet reference resource-server (Cloudflare Workers)

The mandate 402 flow is already a **socket-free pure function**: `serve_once(facilitator,
requirements, request_headers, resource_fn, *, at) -> (status, body, headers)`
(`mandatehub/x402/http402.py`). That shape maps directly onto a Worker's
`request → Response` handler, so the **Python Worker** reuses the library core unchanged rather
than reimplementing it.

[`deploy/cloudflare/`](../deploy/cloudflare/) contains:

- **`wrangler.toml`** — a Python Worker (`compatibility_flags = ["python_workers"]`, `main =
  "worker.py"`), with `mandatehub` vendored/pinned as a Worker dependency and config via vars.
- **`worker.py`** — an `on_fetch` handler that builds the `PaymentRequirements`, calls
  `serve_once` with an in-memory ledger + mandate, and returns the `402` / `200 + proof`
  response. It is a **skeleton**: it parses and mirrors the example flow, but it has **not**
  been run on Workers here (see § Honest status).

Owner runbook:

1. `npm i -g wrangler` and `wrangler login` (your Cloudflare account).
2. From `deploy/cloudflare/`: `wrangler dev` to run it locally on the edge runtime, then
   `wrangler deploy` to publish.
3. For real testnet `/settle`, add the facilitator URL + key as **secrets** (`wrangler secret
   put MANDATEHUB_FACILITATOR_URL`, `... MANDATEHUB_AGENT_PRIVATE_KEY`) — never in
   `wrangler.toml`. Without them the Worker still serves the `402` handshake and the in-ledger
   (mock) settle for a demo.

### State & durability (the real design decision)

A Worker isolate is ephemeral, so an **in-memory** ledger resets between requests — fine for a
stateless *demo* resource-server that quotes `402` and delegates settlement to the Base Sepolia
facilitator, but **not** a durable mandate ledger. A durable deployment binds the
`LedgerStorage` protocol to **Cloudflare D1** (SQLite at the edge) or **Durable Objects** for
the replay-safe, single-writer settlement path. That durable-storage step **is gate H2** in
[ROADMAP](../ROADMAP.md) — so the Worker here is deliberately the *testnet demo* tier, not the
production facilitator.

## Honest status

- The **static site** (Part A) is complete and deployable as-is; you can open `site/index.html`
  locally to see exactly what Pages will serve.
- The **Worker** (Part B) is a **scaffold**: the Python parses and follows the verified
  `serve_once` flow, but it has not been executed on the Workers runtime in this repo. Treat the
  first `wrangler dev` as the confirmation, and expect the state/D1 adaptation above before it
  is anything more than an ephemeral testnet demo. No mainnet value; H1–H3 still gate real
  money.
