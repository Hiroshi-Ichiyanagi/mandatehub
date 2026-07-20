# Best execution & surplus recapture (`execution/`)

The `execution` package is the **verification core** for a "0% fee to the user, yet the
system earns autonomously" gateway — the MEV / arbitrage-recapture direction. It executes
nothing on-chain; it makes an *already-executed* fill **provably best-of-disclosed and
fairly split**.

It is deliberately **standalone**: nothing under `execution/` imports `intent/`
(enforced by a test). The bridge that ties the two together lives on the intent side
(`intent/bridge.py`).

## Pieces

- **`routing.py`** — `RouteQuote` / `select_best_route`: deterministic best-route
  selection over quoted outputs (integer, tie-break by `route_id`).
- **`auction.py`** — `SolverBid` / `run_auction`: multiple solvers bid to fill an intent;
  the winner is the best net for the user (min cost or max out), tie-break by `solver_id`.
  Losers are recorded so a third party can verify the winner beat them.
- **`surplus.py`** — `SurplusSplitPolicy` / `compute_split`: the price-improvement surplus
  is split gas-off-top, then by bps into user rebate / referrer / operator margin. The
  **operator margin is the sole residual** and absorbs all rounding, so `total() == surplus`
  exactly (integer cents, no float). A split is also posted as one balanced transaction, so
  an inexact split is *structurally un-postable*.
- **`arbitrage.py`** — `find_best_arbitrage_cycle`: detect a profitable cycle on a quoted
  pool graph with integer floor propagation (never over-claims). **Memo-only**: it emits an
  `arbitrage_detected` audit event and posts *nothing* to the ledger — booking unrealized
  arbitrage would mint unbacked revenue. Value is booked only when realized through an
  actual fill (via the bridge).
- **`proofs.py`** — `ProofOfBestExecution` (user got ≥ best *disclosed* quote and ≤ their
  limit; the posted split matches policy) and `ProofOfSurplusRecapture` (splits sum exactly;
  the user's effective fee vs. their limit is ≤ 0). Both are deterministic and re-verifiable
  offline.

## The bridge: `settle_via_auction` (3 ↔ 4)

`intent/bridge.py` runs an intent through a solver auction and settles it at the winning
executed cost, splitting the surplus — as **one balanced transaction** — while preserving
every mandate invariant.

The load-bearing idea is **two disjoint value planes**:

| plane | quantity | who reads it |
| ----- | -------- | ------------ |
| **budget** (authorization) | the escrow outflow = the user's `user_limit` | mandate budget / velocity / epoch / collateralization |
| **receipt** (execution) | the payee credit = `executed_cost`; the surplus = `limit − cost` | best-execution + surplus proofs |

Because the budget plane only ever books the escrow outflow (`user_limit`), a
best-executed settlement is **byte-identical to a plain settlement of the limit** on every
budget-side field of `ProofOfMandate` — **INV-9**. The payee legitimately receives less,
and the recaptured surplus lands in USER (rebate) / FEE (margin, gas) / PLATFORM (referrer)
accounts the budget re-derivation never touches.

- **Model A** (`C_in == C_out`, canonical, self-funded): the price improvement is the
  user's own committed headroom; both proofs are emitted.
- **Model B** (`C_in != C_out`, cross-currency): the venue is a flow-through market mirror.
  Because each ledger account holds a single currency, the mirror is **two** `CLEARING`
  accounts — one per currency — and the transaction balances per currency. The surplus is
  denominated in the output currency.

## Scope honesty (what the proofs do *not* claim)

- Best-execution proves **best of the disclosed candidate set**. A suppressed bid, or a
  fully-colluding solver pool that uniformly under-reports, is out of offline scope without
  a live quote oracle — the field is named `user_no_worse_than_best_disclosed`.
- The surplus proof's `user_effective_fee_vs_limit_non_positive` is **relative to the
  user's own limit price**, not to a fair mid/market.
- Model B proves the accounting/split is honest, not that the swap physically executed on
  chain (out of scope by the project's identity).

See [`examples/best_execution_recapture.py`](../examples/best_execution_recapture.py) for a
runnable end-to-end walk-through.
