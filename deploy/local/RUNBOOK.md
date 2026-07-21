# Operator runbook (H2 — durable local service)

Running [`operator.py`](operator.py): the restart-safe, mandate-gated x402 resource server.
Inherits the x402-Gateway operating pattern (launchd residency, file-backed state, honest
logs). Testnet by default; mainnet stays behind H1–H3 ([ROADMAP](../../ROADMAP.md)).

## Start / stop

```bash
# foreground (dev)
export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator
export MANDATEHUB_PAY_TO=0xYourReceivingAddress
python deploy/local/operator.py                 # listens on 127.0.0.1:8402

# resident (launchd) — edit CHANGEME paths/values first
cp deploy/local/com.mandatehub.operator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mandatehub.operator.plist
launchctl unload ~/Library/LaunchAgents/com.mandatehub.operator.plist   # stop
```

## Health & state

```bash
curl -s localhost:8402/healthz | python3 -m json.tool
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

Same as the x402-Gateway: put a Cloudflare Named Tunnel in front of `127.0.0.1:8402`
(never expose the port directly). The tunnel config is owner-side and holds no secrets of
this service.
