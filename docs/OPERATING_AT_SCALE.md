# Operating mandatehub at scale (pricing, revenue, adoption, scaling)

The engineering for a live service is done ([`deploy/local/RUNBOOK.md`](../deploy/local/RUNBOOK.md)).
This is the **operator's** guide to running it as a real service: what to charge, how revenue
flows and is tracked, how agents discover it, and when to scale out. It is operating guidance,
not financial or legal advice; the mainnet H1–H3 gates in [ROADMAP](../ROADMAP.md) still stand.

## 1. Pricing

The price is `MANDATEHUB_AMOUNT` (minor units; `10000` = $0.01 USDC) — the per-call ceiling in
the 402 challenge. Change it and restart; the new price applies to new challenges immediately.

| model | how | when |
| ----- | --- | ---- |
| **Flat per-call** | one `MANDATEHUB_AMOUNT` | default; simplest for one resource |
| **Per-resource** | run one operator per priced endpoint (different port/tunnel) | different products at different prices |
| **Best-execution** | expose the `best-exec` scheme (③): charge "up to X", fill at the best disclosed cost, rebate the surplus | "0% fee to the user, yet the system earns" — a differentiator vs flat facilitators (see [`specs/best-exec.md`](../specs/best-exec.md)) |

Sizing: keep the price ≥ a meaningful multiple of any per-call cost. On mainnet, gas is paid by
the facilitator (EIP-3009 gasless), so the operator's marginal cost per settled call is ~0 —
the price is pure margin minus the facilitator's cut.

## 2. Revenue: where it goes, how it's tracked

- Funds settle **on-chain to `MANDATEHUB_PAY_TO`** (the merchant address) every accepted call.
  That address's USDC balance *is* the revenue; withdraw/custody it as you would any wallet.
- **Live**: `GET /metrics` (or the browser dashboard at `/`) shows settlements, revenue,
  unique payers, and a per-day breakdown; the monitor line carries `revenue=…USDC`.
- **Offline / historical**: `python deploy/local/stats.py [--json]` reports the same from the
  ledger or from any backup snapshot.
- Reconcile the operator's `revenue_cents` against the on-chain balance of `MANDATEHUB_PAY_TO`
  periodically — they should track (the ledger books what the facilitator settled).

The **agent** side (the wallet that *pays*, `MANDATEHUB_AGENT_PRIVATE_KEY`) is separate: keep
it funded for whatever the operator itself needs to pay out (it is not the revenue wallet).

## 3. Adoption — getting agents to use it

- **The client is the library.** Any x402 client pays it; mandatehub ships one:
  `python examples/x402_pay.py https://<host>/quote`. Point developers at
  [the site](https://mandatehub.ichiyanagi1111.workers.dev) and `pip install mandatehub`.
- **x402 Bazaar (Coinbase CDP discovery).** Agents discover resources via
  `GET /platform/v2/x402/discovery/resources`. Listing is **not** automatic from settlements and
  is **not** a public API POST (that returns 404) — register the resource through the **CDP
  portal** (owner action) so it appears with a service name, description, tags, and icon. Until
  then the endpoint is fully usable by anyone with the URL; it's just not in the directory.
- **Self-describing endpoint.** `GET /` returns a JSON info object (service, price, how-to-pay,
  links) for machines and an HTML dashboard for humans — both good landing surfaces to share.

## 4. When (and how) to scale out

The reference operator is **single-process by design** — that is a *correctness* boundary, not
just a perf one: replay/budget safety is trusted because there is exactly one writer. Do **not**
run two operators against the same data.

- **One process is plenty for a lot of traffic** — settlement is the slow step (a facilitator
  round-trip), the mandate gate is in-memory, and denied calls cost nothing. Vertical headroom
  first.
- **Rate limiting** is native: `MANDATEHUB_RATE_PER_MIN` caps settlements/60s (restart-safe,
  fail-closed, → HTTP 429). Set it to protect the budget and the facilitator.
- **Budget sizing**: `MANDATEHUB_BUDGET` is the mandate cap — the maximum the agent can spend
  before the operator denies `BUDGET_EXCEEDED`. Size it to your funded exposure; top it up
  (raise the cap + fund the escrow) as volume grows.
- **Going multi-worker** requires the shared-store change in
  [`docs/MULTIWORKER.md`](MULTIWORKER.md) (a Postgres `LedgerStorage` with the atomic unique-PK
  claim) — designed and empirically validated, backend pending. Only then run >1 worker.

## 5. Pre-scale checklist

- [ ] Price set (`MANDATEHUB_AMOUNT`) and reconciles to intended margin.
- [ ] `MANDATEHUB_PAY_TO` is a wallet you custody; revenue reconciliation cadence set.
- [ ] `MANDATEHUB_BUDGET` sized to funded exposure; escrow funded.
- [ ] `MANDATEHUB_RATE_PER_MIN` set.
- [ ] Backups (`com.mandatehub.backup`) + monitor (`com.mandatehub.monitor`) loaded; alerting
      wired to the monitor's non-zero exit.
- [ ] Tunnel + operator resident (`launchctl list | grep mandatehub`), `/healthz` green.
- [ ] Mainnet only as an informed, self-funded decision (H1–H3 acknowledged).
- [ ] (For real scale) multi-worker Postgres backend built + concurrency-tested first.

## 6. Announcement

Ready-to-adapt copy is in [`ANNOUNCEMENT.md`](../ANNOUNCEMENT.md).
