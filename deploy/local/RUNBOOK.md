# Operator runbook (H2 — durable local service)

Running [`operator.py`](operator.py): the restart-safe, mandate-gated x402 resource server.
Inherits the x402-Gateway operating pattern (launchd residency, file-backed state, honest
logs). Testnet by default; mainnet stays behind H1–H3 ([ROADMAP](../../ROADMAP.md)).

## Start / stop

```bash
# foreground (dev)
export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator
export MANDATEHUB_PAY_TO=0xYourReceivingAddress
python deploy/local/operator.py                 # listens on 127.0.0.1:8403

# resident (launchd) — edit CHANGEME paths/values first
cp deploy/local/com.mandatehub.operator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mandatehub.operator.plist
launchctl unload ~/Library/LaunchAgents/com.mandatehub.operator.plist   # stop
```

## Health & state

```bash
curl -s localhost:8403/healthz | python3 -m json.tool
# ok / mandate / remaining_cents / settled_this_process / denied_this_process / audit_root
```

State (all under `MANDATEHUB_DATA_DIR`, default `~/.mandatehub-operator/`):

| file | what | on restart |
| ---- | ---- | ---------- |
| `ledger.db` | append-only double-entry ledger | re-read; budget re-derived |
| `audit.db` | hash-chained audit log | re-read; lifecycle re-derived |
| `mandate.json` | the mandate's parameters | rehydrated via `rehydrate_mandate` (no double history) |

**Restart guarantee (verified live 2026-07-21):** a real settled payment was made, the
process was SIGKILLed, restarted, and (a) the spent budget survived (`remaining_cents`
carried over), (b) replaying the settled payment's `X-PAYMENT` was denied
`DUPLICATE_INTENT` — the storage layer, not process memory, is the line of defense.
Unit coverage: `tests/test_rehydration.py`.

## Rules (from OPERATIONS.md)

- **Single process only.** In-process serialization is trusted because there is exactly one
  process; the SQLite files are its private state. Scaling out first requires moving the
  uniqueness/atomicity constraints into a shared store (Postgres / D1) — that is the
  remaining H2 step, do NOT just run two operators against the same files.
- **Fail-closed.** Malformed header, mandate deny, facilitator error, unreadable state —
  every failure path returns 402 and settles nothing.
- **The operator holds no agent key.** It is the merchant side; the paying agent's key
  never enters this process or its plist.
- **Money-path changes are PR-based** and land only with the suite green (229 tests).

## Incidents

| symptom | action |
| ------- | ------ |
| `/healthz` down | `launchctl list \| grep mandatehub`; read `operator.log`; relaunch via launchctl |
| facilitator 4xx/5xx | payments deny fail-closed (no state corruption); check facilitator status, retry later |
| disk full / db error | service must stay DOWN until resolved (fail-closed beats fail-open); back up the two .db files before any repair |
| suspected tamper | verify the audit chain hashes; `audit_root` in `/healthz` must be reproducible from `audit.db` |

## Public exposure (optional)

Same as the x402-Gateway: put a Cloudflare Named Tunnel in front of `127.0.0.1:8403`
(never expose the port directly). The tunnel config is owner-side and holds no secrets of
this service.

## Public deployment (the x402-Gateway pattern) — LIVE

The operator runs resident behind a Cloudflare Named Tunnel, exactly like the sibling
`x402.obolpay.xyz` gateway (separate tunnel + launchd job; no port exposed directly).

**Live:** <https://mandatehub.obolpay.xyz> — `/` (info), `/healthz`, `/quote` (402 → pay).
On mainnet the operator settles real USDC via the Coinbase CDP facilitator.

Setup used:
1. `operator.py` under launchd (`com.mandatehub.operator.plist`, port 8403), env pointed at
   the CDP facilitator + `MANDATEHUB_NETWORK=base` + `MANDATEHUB_CDP_KEY_FILE`.
2. A **dedicated** tunnel: `cloudflared tunnel create mandatehub`, a `config-mandatehub.yml`
   with `ingress: mandatehub.<domain> -> http://127.0.0.1:8403`, and
   `com.mandatehub.tunnel.plist` under launchd.
3. DNS: `cloudflared tunnel route dns <TUNNEL-UUID> mandatehub.<domain>` — **route by the
   tunnel UUID, not its name.** On a multi-tunnel account the name form silently bound the
   CNAME to the wrong (existing) tunnel; the UUID form is unambiguous.

**Bot protection note:** the Cloudflare zone challenges the *literal* default
`Python-urllib/x.y` User-Agent with a 403 (identical to the x402 zone). Every real client is
fine — browsers, x402 SDKs, and mandatehub's own `RemoteFacilitatorAdapter` all send a
non-default UA. A bare-urllib script must set any `User-Agent` header.

**Verified end-to-end (2026-07-21):** an internet request to `/quote` → x402 `exact` payment →
**real USDC settled on Base mainnet** (tx `0xad7ff6ac…`) → `ProofOfMandate` in the response;
a replay of the same `X-PAYMENT` was denied `DUPLICATE_INTENT` over the public URL.

## Backups, monitoring & state verification (ops jobs)

Three read-only tools + launchd jobs keep the live service safe and observed
(observation never blocks the money path):

| tool | job (cadence) | what |
| ---- | ------------- | ---- |
| `backup.py` | `com.mandatehub.backup` (hourly) | WAL-safe online snapshot of `ledger.db`+`audit.db`+`mandate.json`; **rejects any snapshot whose audit chain doesn't verify**; keeps the newest `MANDATEHUB_BACKUP_KEEP` (default 48). |
| `monitor.py` | `com.mandatehub.monitor` (5 min) | public `/healthz` up, agent on-chain USDC balance (warn below `MANDATEHUB_MIN_USDC`), launchd services loaded. One status line; non-zero exit on any problem. |
| `verify_state.py` | on demand / incident | re-derive budget + collateralization from storage and **verify the audit hash chain** — trusting only the files. |

```bash
python deploy/local/backup.py                       # -> backups/<UTC-stamp>/ (verified)
MANDATEHUB_AGENT_ADDR=0x… python deploy/local/monitor.py
python deploy/local/verify_state.py                 # STATE CONSISTENT / INVALID
tail -f ~/.mandatehub-operator/{monitor,backup}.log
```

**Revenue & usage:** the operator serves `GET /metrics` (settlements, revenue, unique
payers, per-day breakdown, derived from the ledger); `deploy/local/stats.py` prints the same
report offline from a data dir or a backup (`--json` for machine output). The monitor line
includes `revenue=…USDC total_settled=…`. Both use one shared computation (`_metrics.py`), so
the live endpoint and the offline report can never disagree.

**Restore drill:** stop the operator, copy a snapshot's three files into the data dir, run
`verify_state.py` to confirm, restart. Because all state is re-derived from those files, the
operator resumes with the exact budget/replay/lifecycle from snapshot time.

**Tamper detection is real** (tested): a semantic change to any audit payload breaks
`verify_chain()` (`Event hash mismatch`); a pure JSON-representation change (whitespace) does
not — the chain commits to *content*, not bytes.

