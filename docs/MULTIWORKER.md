# Multi-worker durability (H2 — the remaining piece)

The operator today is **single-process on purpose**: replay/budget safety is re-derived from
the ledger, and in-process serialization is trusted *because there is exactly one writer*.
This document specifies how to make it safe to run **multiple workers** (or scale out to a
shared database), what the invariant is, and why the current code must not simply be run twice.

Status: **built and proven end-to-end.** `mandatehub.storage_postgres.PostgresLedgerStorage`
(the `[postgres]` extra) is a shared-store ledger with the atomic unique-PK claim; the engine
settle path uses it. `tests/test_postgres_storage.py` runs the full engine flow AND 8 real
concurrent processes settling the same intent through the engine on one Postgres — exactly one
settles. Remaining for a full multi-worker *operator*: a shared **audit** store (the money-path
ledger safety is done; audit is still per-process).

## 1. The race (why you cannot just run two operators)

Replay protection is enforced in `_authorize` by **reading** all settled records and checking
`intent_id`/`nonce` are unused, then **writing** the settlement. Across two processes that is a
classic check-then-act race: both read "not settled", both pass, both post — a double-spend.
The settlement `transaction_id` is a random UUID, so its `PRIMARY KEY` uniqueness does **not**
save us: two workers generate different tx ids and both insert.

**Empirically demonstrated** (`tests/test_multiworker_poc.py`, real concurrent processes on
one SQLite DB):

| pattern | of 8 processes settling the *same* intent |
| ------- | ----------------------------------------- |
| current read-check-then-write | **up to 8 settle** (double-spend) |
| atomic unique-PK claim | **exactly 1 settles**; the rest get `IntegrityError` → deny |

## 2. The invariant (OPERATIONS.md "multi-worker rule")

> The final line of defense must live at the storage layer, not in process memory.

Concretely: **settling an intent must include an atomic, unique claim on
`(mandate_id, intent_id)` (and on `(mandate_node, nonce)` when nonces are used), in the same
database transaction as the ledger postings.** The unique constraint makes a second settlement
of the same intent *structurally un-postable* (an `IntegrityError`), on any number of workers —
exactly as the single-process `DUPLICATE_INTENT` check does today, but enforced by the DB.

## 3. Design

### 3.1 A `claims` table + the settle path

Add, in the **same database** as the ledger (so one transaction is atomic over both):

```sql
CREATE TABLE settlement_claims (
    claim_key   TEXT PRIMARY KEY,      -- "settle:{mandate_id}:{intent_id}" and, if used,
                                       -- "nonce:{mandate_node}:{nonce}"
    settled_at  TEXT NOT NULL
);
```

`settle_intent` becomes: within one DB transaction —
1. `INSERT` the claim key(s); on `IntegrityError` → abort and return `DUPLICATE_INTENT`
   (or `NONCE_REUSED`) **without** posting.
2. run the budget/policy/window/velocity checks (still full-ledger reads — correct because the
   claim now serializes concurrent settlements of the *same* intent; cross-intent budget races
   are handled by §3.3).
3. post the balanced ledger transaction + append the audit event.
4. commit.

A crash between claim and commit rolls back the whole transaction (claim included) — no
orphaned claims, no burned intents. The read-check in `_authorize` stays as a fast-path deny
and a defense-in-depth layer; the claim is the *authority*.

### 3.2 The `LedgerStorage` protocol gains one method

```python
class LedgerStorage(Protocol):
    ...
    def begin(self) -> "Transaction": ...          # a unit of work spanning claim + postings
    def try_claim(self, key: str) -> bool: ...      # atomic insert; False if already claimed
```

`SQLiteLedgerStorage` implements `try_claim` with the `settlement_claims` table (validated by
the PoC). A `PostgresLedgerStorage` implements the same protocol with `INSERT … ON CONFLICT DO
NOTHING RETURNING` (or a caught `UniqueViolation`) and `SELECT … FOR UPDATE` where a row lock
is needed. **No engine logic changes** — only the storage binding.

### 3.3 Cross-intent budget under concurrency

Two *different* intents settling at once could each pass a full-ledger budget check and jointly
exceed the cap. Options, cheapest first:
- **Per-mandate serialization**: take a row lock on the mandate (`SELECT … FOR UPDATE` on a
  `mandate_locks` row, or an advisory lock keyed by `mandate_id`) for the settle transaction.
  Simple, correct, and the throughput ceiling is per-mandate — usually fine.
- **Reserve-then-commit**: maintain a running `reserved_cents` the settle transaction
  increments under the lock, so the budget check reads committed + reserved.

Start with per-mandate `FOR UPDATE`; it makes the whole settle transaction atomic per mandate
and is trivial to reason about.

### 3.4 Rollout

1. Land `try_claim` + the `settlement_claims` table on **SQLite** first (backwards-compatible:
   existing ledgers get the table created lazily; the read-check still runs). Ship it; the
   single-process operator is unaffected.
2. Add `PostgresLedgerStorage` (+ `[postgres]` extra, `psycopg`). Test against a real Postgres
   with a **concurrent-process replay + concurrent-budget** suite (the PoC generalized).
3. Only then run >1 worker, behind the tunnel, sharing the Postgres.

## 4. What stays out of scope here

Durable *audit chain* across workers (append serialization) uses the same claim/lock
discipline on the `audit_events` sequence — the `prev_hash` link already forces a total order;
concurrent appends serialize on the `(sequence)` PRIMARY KEY. KMS for signer keys and
horizontal TLS/auth are deployment concerns (H2 hardening list), not this document.

## 5. References

- Invariant: [`OPERATIONS.md`](OPERATIONS.md) "multi-worker rule".
- Proof: [`tests/test_multiworker_poc.py`](../tests/test_multiworker_poc.py).
- Current single-writer guarantees: [`specs/mandate.md`](../specs/mandate.md) §6,
  [`tests/test_rehydration.py`](../tests/test_rehydration.py).
