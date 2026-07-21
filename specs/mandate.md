# mandate model (intent / account abstraction)

**Status:** draft · **Version:** 1 · **Direction:** ④ (intent / account abstraction) ·
**Pairs with:** [`best-exec.md`](best-exec.md) (direction ③, the execution plane)

This document specifies mandatehub's **mandate model**: a pre-funded, budget-bounded
authorization that lets an autonomous agent settle intents within it, and a `ProofOfMandate`
that lets anyone verify — **offline, deterministically** — that the agent never exceeded the
budget, stayed inside its policy, and was honestly collateralized. It is the "deposit a
budget, let the agent spend within it" model (ERC-4337 / session-key style), reduced to its
**accounting core** and made provable.

It reuses no network or chain: the guarantee is over an append-only double-entry ledger and a
hash-chained audit log. The on-chain execution layer (bundler / paymaster / session keys) sits
*around* this core (§12).

## 1. Honest boundary (read this first)

A `ProofOfMandate` proves the **accounting**, offline: that for a fixed ledger state at a fixed
time, the settled spend under a mandate (and its whole delegation subtree) never exceeded the
effective budget, every settlement satisfied the mandate's policy, and the escrow held enough
collateral to cover every outstanding commitment on it. It does **not** prove anything about
the outside world:

- **Determinism is on the time axis.** Same ledger state + same explicit `snapshot_at` →
  byte-identical proof and hashes. Identifiers are UUID-based, so two *independently rebuilt*
  ledgers do not share hashes; the guarantee is re-derivation of a fixed state, not a global
  content address.
- **Collateralization is per-escrow-group.** `is_collateralized` means the escrow balance
  covers the remaining commitments of every root mandate sharing that escrow (§7). It is not a
  claim that any chain custodies the funds.
- **Policy is honest, not absolute.** The proof attests the *disclosed* settlements on this
  ledger. It cannot attest a spend that was routed around this ledger, nor legal/contractual
  facts about the principal or payee.
- **No on-chain execution, no HTTP, no keys** in this core — those are the layer around it
  (§12). Moving real value is gated on H1–H3 in [ROADMAP](../ROADMAP.md).

## 2. Roles

- **Principal** — the human/organization that funds the escrow and delegates a budget.
- **Agent (delegate)** — the autonomous party that settles intents against the mandate. It may
  hold a **sub-mandate** (session key) with a bounded slice of the parent budget (§8).
- **Payee** — the counterparty credited by a settlement.
- **Verifier** — anyone (auditor, counterparty, the principal) who checks a `ProofOfMandate`
  offline with only this library and the proof.

The authority is the `IntentSettlementEngine`, which posts every settlement as a balanced
double-entry transaction and every state change as an audit event, both at an **explicit
time**. Audit events require the engine to be constructed with an `AuditLog` (`audit_log=`);
without one, lifecycle events (pause/revoke/top-up) leave no trace and the lifecycle fold in
§9 has nothing to enforce — a deployment that wants lifecycle or auditability MUST supply it.

## 3. The `Mandate`

A mandate is an immutable (`frozen`) record. Its fields (`mandatehub/intent/mandate.py`):

| field | type | meaning |
| ----- | ---- | ------- |
| `mandate_id` | `str` | unique id; MUST NOT contain `/` (the delegation-path delimiter) |
| `principal_id` | `str` | who delegated the budget |
| `escrow_account_id` | `str` | the pre-funded collateral account the spend draws from |
| `currency` | `Currency` | the single currency of this mandate |
| `budget_cap` | `Money` | base budget ceiling; MUST be positive and match `currency` |
| `allowed_purposes` | `frozenset[str]` | non-empty allowlist of purposes a settlement may declare |
| `valid_from` / `valid_until` | `datetime` | authorization window; `valid_until >= valid_from` |
| `created_at` | `datetime` | explicit creation time |
| `per_transaction_limit` | `Money \| None` | optional per-settlement ceiling; positive, same currency |
| `parent_mandate_id` | `str \| None` | set for sub-mandates; `None` for a root (§8) |
| `spend_policy` | `SpendPolicy \| None` | optional payee/amount/sub-budget/velocity/epoch policy (§6.1) |
| `nonce_required` | `bool` | if true, every settlement MUST carry a strictly-increasing nonce |

Construction is fail-closed: an id containing `/`, a non-positive or currency-mismatched
`budget_cap`/`per_transaction_limit`, `valid_until < valid_from`, or an empty
`allowed_purposes` each raise `MandateError`.

**Collateralization at creation.** `create_mandate(...)` requires
`escrow.balance(as_of=created_at) >= budget_cap`; otherwise `MandateError`. A root mandate is
never created undercollateralized. `top_up_mandate(...)` raises the *effective* cap by posting
real collateral into the escrow (§9).

## 4. Two value planes (INV-9)

Every settlement records **two disjoint quantities** (`SettlementRecord`,
`mandatehub/intent/settlement.py`):

| plane | field | read by |
| ----- | ----- | ------- |
| **budget** (authorization) | `authorized_outflow_cents` — the escrow outflow | budget / velocity / epoch / collateralization (§6) |
| **receipt** (execution) | `payee_receipt_cents` — the payee credit | payee-receipt proofs, and the best-exec / surplus proofs of ③ |

For a plain settlement the two are equal. For a best-executed settlement
(`settle_via_auction`, spec [`best-exec.md`](best-exec.md)) the budget plane still books the
user's **limit** while the payee receives the better **executed cost** — so, by **INV-9**, a
best-executed settlement is byte-identical to a plain settlement of the limit on *every
budget-side field of `ProofOfMandate`*. Every invariant in §6 is evaluated on the **budget
plane only**.

## 5. Settlement

### 5.1 `settle_intent`

```
settle_intent(*, mandate_id, intent_id, payee_account_id, amount, purpose, at,
              nonce=None) -> IntentSettlementResult
```

The engine authorizes `amount` against the mandate at explicit time `at` (§6). On accept it
posts **one balanced transaction** — escrow `-amount`, payee `+amount` — tagged
`transaction_type = "INTENT_SETTLEMENT"`, and appends an `intent_settled` audit event. On deny
it posts **nothing** to the ledger and appends an `intent_denied` event. The result is
`IntentSettlementResult(decision ∈ {"SETTLED","DENIED"}, reason, remaining_after_cents,
transaction_id | None, …)`. A denied settlement never moves money — fail-closed.

`preauthorize(...) -> (ok, reason, remaining_before)` runs the identical check **with no side
effects** (the ledger is never touched) — this is what an x402 facilitator's `verify` calls.

### 5.2 `settle_batch`

```
settle_batch(*, mandate_id, intents: Sequence[IntentRequest], at) -> BatchSettlementResult
```

**All-or-nothing.** Each intent is authorized in order, each seeing the prior intents in the
same batch as already-settled (so intra-batch budget/replay is enforced). If any intent is
denied, the whole batch is denied and **nothing** is posted; the result reason is
`"<reason>@<failing_intent_id>"`. On success a single balanced transaction carries all legs,
with the canonical-JSON batch descriptor in its metadata.

### 5.3 `settle_via_auction`

Bridges direction ④ to ③: authorizes the user's `user_limit` on the budget plane, then
best-executes on the receipt plane and recaptures the surplus in one balanced transaction. Full
specification in [`best-exec.md`](best-exec.md); INV-9 (§4) is what keeps its `ProofOfMandate`
budget-side identical to a plain settlement of the limit.

## 6. Invariants (the authorization decision)

`settle_intent` / `preauthorize` evaluate a fixed, canonical sequence of checks and **return on
the first failure**, so a denial reason is deterministic regardless of how many conditions a
request violates. The order (`DENIAL_ORDER`, `mandatehub/intent/mandate.py`) and each check:

| # | reason | denied when |
| - | ------ | ----------- |
| 1 | `CURRENCY_MISMATCH` | `amount.currency != mandate.currency` |
| 2 | `NON_POSITIVE_AMOUNT` | `amount` not positive |
| 3 | `MANDATE_REVOKED` | lifecycle state is `REVOKED` (terminal) |
| 4 | `MANDATE_EXPIRED` | `at > valid_until` |
| 5 | `MANDATE_PAUSED` | lifecycle state is `PAUSED` |
| 6 | `OUTSIDE_WINDOW` | `at < valid_from` or `at > valid_until` |
| 7 | `NON_MONOTONIC_TIME` | `at <` the latest prior `settled_at` on this leaf (anti-backdating) |
| 8 | `PURPOSE_NOT_ALLOWED` | `purpose ∉ allowed_purposes` |
| 9 | `PAYEE_NOT_ALLOWED` | `spend_policy.payee_allowlist` set and payee ∉ it |
| 10 | `BELOW_MIN_AMOUNT` | `amount < spend_policy.min_amount_cents` |
| 11 | `ABOVE_MAX_AMOUNT` | `amount > spend_policy.max_amount_cents` |
| 12 | `PER_TX_LIMIT_EXCEEDED` | `amount > per_transaction_limit` |
| 13 | `NONCE_REUSED` | this `nonce` already settled on this leaf |
| 14 | `NONCE_NOT_INCREASING` | `nonce_required` and nonce missing, or `nonce <=` the prior max |
| 15 | `DUPLICATE_INTENT` | this `intent_id` already settled on this leaf |
| 16 | `SUB_BUDGET_EXCEEDED` | per-purpose sub-budget for `purpose` would be exceeded |
| 17 | `EPOCH_VELOCITY_EXCEEDED` | settlement **count** in the current epoch would exceed its cap |
| 18 | `WINDOW_VELOCITY_EXCEEDED` | settlement count in the rolling window would exceed its cap |
| 19 | `EPOCH_CAP_EXCEEDED` | spend in the current epoch would exceed its cap |
| 20 | `WINDOW_CAP_EXCEEDED` | spend in the rolling window would exceed its cap |
| 21 | `PARENT_BUDGET_EXCEEDED` | some ancestor's effective cap would be exceeded by its subtree (§8) |
| 22 | `BUDGET_EXCEEDED` | this mandate's subtree spend would exceed its effective cap |

Two load-bearing points:

- **Every quantity is re-derived from the ledger / audit chain**, not from in-process counters:
  prior spend, prior nonces, prior intent ids, epoch counts, and window sums are recomputed
  from **all** settled records on the ledger — deliberately a full-ledger read, *not* bounded
  `<= at`, since an as-of read would let a backdated `at` hide later settlements and evade the
  monotonic-time guard. The lifecycle fold and the epoch/window filters then apply their own
  time bounds. Restarting the process changes nothing.
- **`NON_MONOTONIC_TIME` (7) precedes the caps.** A settlement whose `at` predates an earlier
  one is rejected outright — a backdated timestamp cannot be used to scatter spend across
  epochs/windows to slip under a velocity or spend cap.

### 6.1 `SpendPolicy` and `EpochSpec`

The optional `spend_policy` (`mandatehub/intent/policy.py`) supplies checks 9–11 and 16–20
(12 comes from the mandate's own `per_transaction_limit`, 13–14 from `nonce_required`, and 15
is unconditional): `payee_allowlist`, per-purpose `purpose_sub_budgets`, `min/max_amount_cents`, and two independent
rate limiters — an **epoch** limiter (`EpochSpec` + `epoch_spend_cap_cents` /
`epoch_settlement_cap`) and a **rolling-window** limiter (`rolling_window_seconds` +
`*_spend_cap_cents` / `*_settlement_cap`). `EpochSpec.epoch_index(at)` is computed in **integer
microseconds since a fixed anchor** (`days*86_400_000_000 + seconds*1_000_000 + microseconds`,
floor-divided by the epoch length) — never `timedelta.total_seconds()`, which is a float. The
policy rejects an inconsistent config (a cap without its limiter) at construction with
`MandateError`.

## 7. `ProofOfMandate` — offline re-verification

`ProofOfMandateGenerator(engine).generate(mandate_id, snapshot_at) -> (ProofOfMandate,
MerkleTree)` produces a proof for the mandate as of an **explicit** `snapshot_at`. Core fields
(`mandatehub/intent/proofs.py`):

| field | meaning |
| ----- | ------- |
| `snapshot_at` | the explicit as-of time the whole proof is computed at |
| `mandate_id`, `principal_id`, `currency_code` | identity |
| `budget_cap_cents` / `effective_budget_cap_cents` | base cap / base + top-ups (§9) |
| `total_settled_cents` | budget-plane spend of this **leaf** mandate |
| `aggregate_settled_incl_descendants_cents` | budget-plane spend of the whole **subtree** |
| `remaining_cents` | `effective_cap − subtree_settled` |
| `settlement_count`, `payee_count` | distinct intents / payees |
| `escrow_account_id`, `escrow_balance_cents`, `co_escrow_remaining_cents` | collateral view |
| **`is_within_budget`** | `remaining_cents >= 0` |
| **`is_collateralized`** | `escrow_balance_cents >= co_escrow_remaining_cents` |
| `payee_receipts_root` | Merkle root over the sorted per-payee receipt-plane credits |
| `session_tree_root`, `sub_mandate_ids` | Merkle root + ids over the delegation subtree (§8) |
| `per_epoch_spend`, `remaining_*_cap_cents`, `remaining_*_velocity` | enriched policy state |
| `lifecycle_state` | `ACTIVE` / `PAUSED` / `REVOKED` / `EXPIRED` as of `snapshot_at` |
| `valid_from`, `valid_until` | the window |
| `audit_log_root_hash` | as-of commitment `audit_root_as_of(snapshot_at)` over all events `<= snapshot_at` |

What a verifier reads:

- **Within budget.** `is_within_budget` with `remaining_cents = effective_budget_cap_cents −
  aggregate_settled_incl_descendants_cents >= 0` — the agent and its whole delegation subtree
  never exceeded the (topped-up) budget.
- **Collateralized.** `is_collateralized` — the escrow balance covers the outstanding
  commitments of **every** root mandate sharing that escrow (`co_escrow_remaining_cents`), so
  over-funding one mandate cannot mask under-funding a co-tenant of the same escrow. The
  fleet-wide `MandatePortfolioProof` extends this: `is_collateralized` there holds only if
  **each escrow group is individually collateralized**.
- **Merkle inclusions.** `payee_receipts_root` and `session_tree_root` let a party prove a
  single payee's receipt, or a single sub-mandate's **subtree** settled total (own +
  descendants), is included — without the
  full ledger.
- **As-of audit commitment.** `audit_log_root_hash` is the hash-chain root over exactly the
  events at `timestamp <= snapshot_at` — *not* the log's latest hash — so the same state at the
  same time always commits to the same value, and any later event cannot alter a past proof.

The proof reads the wall clock **nowhere**: every field derives from ledger records and audit
events filtered by the explicit `snapshot_at`.

## 8. Session keys / sub-mandates (non-leakage)

`create_sub_mandate(...)` delegates a bounded slice of a parent budget. Constraints at
creation (each a `MandateError` on violation): parent is `ACTIVE` at `created_at`; child
currency equals parent's; child window ⊆ parent window; child `allowed_purposes` ⊆ parent's;
delegation depth `<= MAX_DELEGATION_DEPTH` (8). A sub-mandate **shares the parent's escrow** —
it draws on the same collateral, it does not add new collateral.

The non-leakage guarantee is structural. At authorization, check 21 (`PARENT_BUDGET_EXCEEDED`)
re-derives, **for every ancestor**, the total budget-plane spend of that ancestor's entire
descendant set and denies if `ancestor_settled + amount > ancestor_effective_cap`. So the
combined spend of all descendants of any node can never exceed that node's cap — a session key
cannot, alone or in concert with siblings, spend more than the parent authorized. Aggregation
is by **set membership on ids** (`descendant_ids` / `ancestor_ids`), never substring matching,
so `root/a` and `root/ab` are distinct and never collide. `session_tree_root` (§7) commits the
subtree and each node's **subtree** settled total (own + descendants) for offline inspection.

## 9. Lifecycle

Lifecycle state is **folded from the audit chain**, never stored mutably. From the events
`pause` / `resume` / `revoke` / `top_up` (and the window), `fold_lifecycle(at)` derives
`MandateState`: `REVOKED` is terminal; else `EXPIRED` if `at > valid_until`; else `PAUSED` if
currently paused; else `ACTIVE`. Events are ordered by `(timestamp, sequence)` for a
deterministic fold. `top_up_mandate(...)` posts real collateral from a funding account into the
escrow and raises the **effective** cap by the topped-up amount; `effective_budget_cap_cents =
budget_cap_cents + Σ top-ups`. Because state is a pure function of the audit chain up to `at`, a
proof at any past `snapshot_at` reflects exactly the lifecycle then in force.

## 10. Determinism discipline

- **Explicit time everywhere.** `create_mandate(created_at=…)`, `settle_intent(at=…)`,
  `generate(snapshot_at=…)`, `fold_lifecycle(at=…)` — no method reads `datetime.now()`. The
  **test suite** enforces this: a runtime-guard test patches `datetime.now` to raise while
  exercising the settle / auction / proof paths, and a static (AST) guard test forbids the
  listed deterministic modules from reading the wall clock or `total_seconds`.
- **Integer minor units only.** All money is integer cents; every transaction balances to zero
  per currency; a surplus leak is structurally un-postable.
- **As-of commitment, not latest.** Audit roots are taken `as_of(snapshot_at)`, so a proof is a
  function of (state, time), reproducible by a third party byte-for-byte.

## 11. Error catalog

- **`MandateError`** — a structural / configuration error (the caller's bug): invalid mandate
  fields, insufficient collateral at creation, a sub-mandate that violates a parent constraint,
  or an inconsistent `SpendPolicy` / `EpochSpec`.
- **`SettlementIntegrityError`** — a ledger/metadata contradiction discovered while
  re-deriving records (missing escrow tag, escrow-flow or payee-credit mismatch, batch
  multiset mismatch). Fail-closed: an unreadable settlement is rejected, never guessed.
- **Denial reasons** (returned, not raised, in `IntentSettlementResult.reason`) — the 22
  strings of §6, in `DENIAL_ORDER`.

## 12. On-chain boundary (out of core)

This spec covers the **accounting**. A real deployment binds it to chain state:

- The escrow draw-down here **models** an on-chain pull (ERC-4337 paymaster / session key). A
  real facilitator supplies a `SettlementAdapter` (the seam defined in
  `mandatehub/x402/facilitator.py`; an on-chain variant is out of core) that moves value; the
  mandate gate and proofs wrap it unchanged (see [`../docs/X402.md`](../docs/X402.md)).
- The `best-exec` execution plane's on-chain settler is an **unbuilt, MUST-be-audited**
  contract — gate **H1** ([`best-exec.md` §7](best-exec.md)).
- Durable storage (Postgres via the `LedgerStorage` protocol), key management, auth, and rate
  limiting are gate **H2**; legal/compliance review is **H3**. Only after H1–H3 does real value
  move ([ROADMAP](../ROADMAP.md)).

## 13. References

- mandatehub: [`docs/INTENT_MANDATES.md`](../docs/INTENT_MANDATES.md) ·
  [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) ·
  [`docs/EXECUTION_RECAPTURE.md`](../docs/EXECUTION_RECAPTURE.md) · the execution-plane spec
  [`best-exec.md`](best-exec.md) · implementation `mandatehub/intent/`
- Account abstraction: [ERC-4337](https://eips.ethereum.org/EIPS/eip-4337)
- [x402](https://github.com/coinbase/x402) — the HTTP-402 protocol this model gates
