# Operating mandatehub — the x402-Gateway way

This project inherits the operating discipline of a production x402 USDC gateway (build →
productionize → harden → monitor → hands-off). This doc adapts that playbook to mandatehub.
The **charter (§0)** and **methodology (§3)** apply *now*, to every change; the deployment /
monitoring sections apply when the library becomes a **running facilitator**.

## Two artifacts, two trust levels

Do not conflate them — they have opposite disclosure rules:

| | `mandatehub` (this repo) | the facilitator deployment (future, separate) |
| --- | --- | --- |
| what | the library / protocol (like `coinbase/x402`) | a running service (FastAPI + keys + VPS + Cloudflare Tunnel) |
| visibility | **public**, Apache-2.0 | **private** (holds `.env`, signer keys, receipt DB) |
| secrets | **none** — stdlib-only, no `.env`, no keys | real; `.env` via pydantic-settings, never committed |
| moves money | no (proves accounting offline) | yes (gated on H1–H3) |

The library is safe to open-source today. The playbook's secret-hygiene, VPS/Docker/Tunnel,
and monitoring rules bind the **deployment** repo, not this one.

## Charter (applies to every change)

- **Money / production / outward-facing ops always get confirmation.** No unilateral
  irreversible action. Propose → approve → execute — especially money/secret.
- **fail-closed.** Silence, error, or "unknown" resolves to deny/hold. (mandatehub already
  does this: unreadable settlement → reject; stale/over-budget/replay → deny.)
- **Honest reporting.** Tests that fail are reported as failing; numbers come from real
  artifacts (`pytest`, example output), never simulated. See playbook §3.7.
- **Incremental on the money-path.** Never batch many settlement-path changes; safe change →
  verify → behavior change.
- **Observation never blocks the main flow.** Notifications/analytics are best-effort
  (fire-and-forget, swallow-and-log); a payment that settled stays settled.

## Money-path invariants (playbook §2 → what mandatehub already enforces)

The playbook's money-path rules are already the spine of mandatehub — keep them intact:

- **Signature binding** (`{domain}:{invoice}:{tx}` in the playbook) → the `best-exec` **nonce
  commits to the whole binding** (`{chainId:settler:asset:maxAmount:payTo:rebateTo:operatorTo:
  splitPolicyHash:objective:intentId:window}`), so an authorization can't be replayed or
  repointed. `verify` cross-checks **every** bound field, not just the digest.
- **Replay prevention at the storage layer** (playbook: DB primary key + IntegrityError — "the
  only line that holds across processes") → per-mandate-node nonces + intent-uniqueness +
  **monotonic settlement time**, all re-derived structurally from the append-only ledger /
  audit chain, not from in-process state.
- **Conditional-atomic settlement** (playbook: `UPDATE … WHERE nonce=:n AND balance>=:p`,
  `rowcount==1`) → double-entry that **balances to zero per currency**; a surplus leak is
  *structurally un-postable*, and the budget check is fail-closed before any credit.
- **fail-closed freshness / limits** → window (`OUTSIDE_WINDOW`), budget (`BUDGET_EXCEEDED`),
  gas-vs-surplus (`GAS_EXCEEDS_SURPLUS`) all deny rather than guess.

**Multi-worker rule (playbook's sharpest lesson):** in-process locks break the moment you add
a worker. When the facilitator scales out, the final line of defense must stay at the storage
layer — Postgres via the `LedgerStorage` protocol with the uniqueness/atomicity constraints
above (gate H2). Never rely on in-process serialization alone.

## Methodology (playbook §3 — the core to inherit)

- **Guardrails (§3.1).** After the initial R1 seed push, the money-path / facilitator work is
  **PR-based — no direct push to main**; Claude proposes, a human approves, then merge. No
  unilateral broad money/secret changes.
- **test → (deploy) → verify, every change (§3.2).** `py_compile`/import → functional test
  (real behavior, both happy *and* error paths for the money-path) → for the service, deploy
  then verify externally (public URL) *and* internally (container/logs/config). This repo's
  half is already enforced: 249 tests + runnable examples + determinism/import guards.
- **Staged deploy (§3.3).** Layer by risk: Stage 1 = behavior-invariant hardening, Stage 2 =
  behavior-changing — separate deploys, each verified.
- **Drift monitoring (§3.4).** For the deployment: sha256 source vs the deployed `/app` to
  catch "hand-patched prod" / "forgot to deploy".
- **Don't trust reviews — re-verify (§3.6).** Every review finding is re-checked before a fix
  (this is exactly how the best-exec gaps were found and confirmed, and how a false "bug" is
  avoided). Adversarial multi-lens review is the method, not a rubber stamp.

## Roadmap mapping (playbook phase → mandatehub track)

| playbook phase | mandatehub |
| --- | --- |
| 1 core / 2 E2E | done (library) + **P-live** testnet E2E (`x402_live_smoke.py` on Base Sepolia) |
| 3 productionize (VPS + Docker Compose + Cloudflare Tunnel) | **R5** deployment repo + edge (Cloudflare Pages/Workers) |
| 4 harden-1 (startup fail-closed, rate-limit, secret non-exposure) | **H2** production hardening |
| 6 discovery (machine-readable manifest, directory listing) | facilitator manifest at R5+ |
| 7 monitoring automation (liveness/freshness/backup/deep-audit) | **H2** ops; quiet-unless-anomaly |
| 8 deep multi-lens security review | **H1** independent audit (incl. the on-chain `BestExecSettler`) |
| 9 versioning + tests + CI | done (`ci.yml`, guards); Dependabot to enable |
| 10 hands-off / data-driven | after H1–H3; iterate on real demand, not on zero traffic |

## Operational invariants (playbook §6 — must hold; not all are in code)

1. **Core stays stdlib-only**; crypto/network isolated behind the `[evm]` extra and adapters.
2. **Never `datetime.now()` on a proof/settlement path** (enforced by static + runtime guards).
3. **No real money until H1 (audit) + H2 (hardening) + H3 (legal).** `settlementPlane:"in-ledger"`
   and `StubVerifier`/`StubSigner` are honesty markers — a facilitator MUST fail-closed rather
   than run stubs against mainnet (the mandatehub analog of the playbook's "TEST_MODE in prod"
   trap, §9-12).
4. **Secrets only via env / keystore**, never hardcoded or logged; the origin is never exposed
   directly (Cloudflare Tunnel only) once deployed.
5. **fail-closed has a cost:** stalling a payment can halt a paid service — pair every
   fail-closed gate with freshness monitoring that pages fast (§9-13).

## Carry-over gotchas for our deployment (playbook §9)

- Prevent secret commits: `.gitignore` first, scan the staging area, and **verify the remote
  has no `.env` (404) after push** — not just "unstaged".
- Behind Cloudflare, use `CF-Connecting-IP` for rate-limit buckets (origin non-exposed is the
  precondition); make edge WAF the primary limiter, app-side the second layer.
- Co-bump interlocked deps (a single Dependabot PR that splits them goes red); Dependabot's
  internal update jobs are **not** project CI — exclude them from CI-health checks.

*Source: the x402 Gateway build playbook (2026-07). Read it for the full detail; this file is
the mandatehub-specific adaptation.*
