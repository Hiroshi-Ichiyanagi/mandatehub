# `best-exec` scheme (x402)

**Status:** draft · **Version:** 1 · **Scheme id:** `best-exec` · **Depends on:** the x402
`exact` scheme (EIP-3009 `transferWithAuthorization`)

This document specifies the `best-exec` x402 scheme: a facilitator best-executes a payment
within the price the resource server named, and returns the price-improvement **surplus** to
the user, split by an agreed policy. It exposes mandatehub's solver auction + surplus
recapture (direction ③) as an x402 scheme alongside `exact`, so a resource server can charge
"up to *X*" and the agent provably pays "the best of the disclosed fills, never more than
*X*" — with the surplus recaptured honestly.

It reuses the [x402](https://github.com/coinbase/x402) roles, vocabulary, and header protocol
(`X-PAYMENT` / `X-PAYMENT-RESPONSE`) unchanged. Only the *scheme* is new.

## 1. Honest boundary (read this first)

`best-exec` proves the **accounting**, offline: that whoever executed stayed within the
user's max, filled at the best of the *disclosed* candidates, split the surplus
integer-exactly, and produced self-consistent proofs. It does **not**, by itself, prove that
funds moved on chain — that is one **online** step (read the settlement tx; §6 step 9).

- On-chain safety (atomic split, no rebate withholding, nonce binding) depends on an
  **audited `BestExecSettler` contract** — **unbuilt, out of scope for this core, MUST be
  audited** before any real value moves (§7).
- The offline binding/policy commitment is **sha256** (Python's standard library has no
  keccak256); the on-chain authorization nonce is **keccak256**. These are two distinct hash
  domains over the *same* preimage (§4). Offline artifacts verify the *logic*; they do not
  reproduce the literal on-chain nonce.
- The guarantee is `user_no_worse_than_best_disclosed` (best of the disclosed set, not the
  global market optimum) and `user_effective_fee_vs_limit_non_positive` (≤ 0 vs the user's own
  limit). A suppressed bid or a fully-colluding solver pool is out of offline scope.

## 2. Roles

Same three roles as x402. The **facilitator** additionally runs (or reads) a solver auction
and holds the mandate that funds the payment. `bids` (the disclosed `SolverBid`s) are gathered
by the facilitator out of band and are inputs to `verify` / `settle`.

## 3. `PaymentRequirements` — the `bestExec` extra

A `best-exec` requirement is an ordinary x402 `PaymentRequirements` with `scheme:"best-exec"`
and an `extra.bestExec` object (`BestExecParams.to_wire()`):

```jsonc
{
  "scheme": "best-exec",
  "network": "base-sepolia",
  "maxAmountRequired": "100000000",          // the ceiling; EIP-3009 value is fixed to this
  "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  // USDC (Base Sepolia)
  "payTo": "0x…",                            // liquidity venue / payee
  "resource": "https://api.example/execute",
  "maxTimeoutSeconds": 60,
  "extra": {
    "name": "USDC", "version": "2",          // EIP-712 domain (from the exact scheme)
    "bestExec": {
      "v": 1,
      "objective": "MIN_COST",               // "MIN_COST" | "MAX_OUT"
      "settler": "0xBestExecSettler…",        // EIP-3009 `to` — audited settler CONTRACT (§7)
      "settlerCodeHash": "0x…",              // pin the audited bytecode
      "splitPolicy": { "user_rebate_bps": 7000, "operator_margin_bps": 3000,
                       "referrer_bps": 0, "gas_reimbursement_cents": 0 },
      "rebateTo": "0x…",                      // user's rebate sink
      "operatorTo": "0x…",                    // operator margin sink (sole residual absorber)
      "currency": { "in": "USDC", "out": "USDC" },
      "mandateRef": "m1",                    // REQUIRED for the in-ledger facilitator (§5)
      "purpose": "X402_BEST_EXEC",
      "fallbackScheme": "exact"              // if best-exec can't improve, fall back
    }
  }
}
```

`user_rebate_bps + operator_margin_bps + referrer_bps` MUST equal 10000 (the split policy
rejects any other sum at construction). `gas_reimbursement_cents` is taken off the top of the
surplus before the bps split.

## 4. Nonce binding

The load-bearing security primitive. The EIP-3009 authorization has a **fixed** `value`
(= `maxAmountRequired`) and its `nonce` is set to a **commitment over the full best-exec
binding**:

```
binding = {
  chainId, settler, asset, maxAmount, payTo, rebateTo, operatorTo,
  splitPolicyHash, objective, intentId, validAfter, validBefore
}
nonce = keccak256(canonical(binding))     // on-chain
      = sha256(canonical(binding))        // offline artifacts (this core)
splitPolicyHash = keccak256/sha256(canonical(splitPolicy))
```

`canonical(·)` is JSON with sorted keys and no insignificant whitespace. Because the signed
`nonce` *is* the digest of the binding, an operator cannot repoint funds (`payTo`), skim the
rebate (`rebateTo`/`operatorTo`), swap the settler, change the objective, widen the window, or
alter the split without invalidating the user's signature. Verifiers MUST check **both** that
`nonce == digest(binding)` **and** that every binding field matches the requirements /
authorization / facilitator config — the digest alone is insufficient, since an attacker who
tampers a field can recompute a self-consistent nonce (see
`test_policy_mismatch_when_attacker_self_consistently_rebinds`).

## 5. `PaymentPayload`

```jsonc
{
  "x402Version": 1,
  "scheme": "best-exec",
  "network": "base-sepolia",
  "payload": {
    "signature": "0x…",                     // EIP-712 sig over the EIP-3009 authorization
    "authorization": {                       // exact-scheme EIP-3009 fields
      "from": "0x…", "to": "0xBestExecSettler…",
      "value": "100000000",                 // == maxAmountRequired (fixed)
      "validAfter": "…", "validBefore": "…",
      "nonce": "0x…"                         // == digest(bestExecBinding)
    },
    "bestExecBinding": { …the binding preimage from §4… }
  }
}
```

### 5.1 Funding plane

The reference facilitator in this core is **in-ledger**: `mandateRef` is REQUIRED and the
escrow draw-down of the referenced mandate models the on-chain pull. `verify` fails closed
with `MANDATE_REF_REQUIRED` (absent) or `MANDATE_REF_MISMATCH` (not this facilitator's
mandate). A real on-chain facilitator supplies an `OnChainAdapter` (out of core) instead.

## 6. `verify` and `settle`

`verify(requirements, payload, bids, at, on_chain=False) -> (ok, reason)` runs, in order:

1. `VALUE_NOT_MAX` — `authorization.value == maxAmountRequired`.
2. `OUTSIDE_WINDOW` — `validAfter ≤ at ≤ validBefore`.
3. `POLICY_MISMATCH` — `nonce == digest(binding)` **and** every binding field matches
   requirements / `bestExec` / authorization / this facilitator's `chainId` (§4).
4. `MANDATE_REF_REQUIRED` / `MANDATE_REF_MISMATCH` (§5.1).
5. On-chain guards (`on_chain=True` only): `CROSS_CURRENCY_NOT_SUPPORTED` (`in != out`),
   `MAX_OUT_NOT_SUPPORTED_ONCHAIN` (`objective == MAX_OUT`).
6. Mandate pre-authorization — the side-effect-free budget/policy/window/replay check
   (`BUDGET_EXCEEDED`, `DUPLICATE_INTENT`, …).
7. Auction feasibility — a winning bid exists (`NO_WINNING_BID`) and gas does not exceed the
   surplus (`GAS_EXCEEDS_SURPLUS`).

`settle(...)` re-runs `verify`, then executes **one balanced transaction** via
`settle_via_auction`: the escrow pays `maxAmount` on the **budget plane**; on the **receipt
plane** the payee receives `executedCost` and the surplus (`maxAmount − executedCost`) is split
to `rebateTo` / `operatorTo` / gas / referrer integer-exactly (the operator absorbs any
rounding residual). The response carries `executedCost`, the `split`, the `auction`
(`winnerId`, `candidatesMerkleRoot`, disclosed `candidates`), `proofOfBestExecution`,
`proofOfSurplusRecapture`, the `binding` + `nonce`, and `settlementPlane` (`"in-ledger"` for
the reference adapter — it never claims funds moved on chain).

### 6.1 Two value planes (INV-9)

| plane | quantity | who reads it |
| ----- | -------- | ------------ |
| **budget** | escrow outflow `= maxAmount` | mandate budget / velocity / epoch / collateral |
| **receipt** | payee credit `= executedCost`; surplus `= maxAmount − executedCost` | best-exec + surplus proofs |

Because the budget plane only ever books `maxAmount`, a best-executed settlement is
byte-identical to a plain `exact` settlement of the limit on every budget-side proof field —
while the payee receives the better executed cost and the surplus is recaptured on the other
plane.

## 6.2 Offline re-verification (step 9 = on-chain, excluded)

`verify_best_exec_response(response, agreed_policy) -> {check: bool}` lets a third party
recompute the accounting from the response alone, standard-library only:

- `winner_matches_rerun` — re-run the auction over the disclosed candidates.
- `executed_cost_matches_winner` — `executedCost` equals the winning fill (MIN_COST).
- `independent_no_worse_than_disclosed` — recomputed **independently** from the candidates;
  it does **not** trust the proof's self-reported flag (a forged
  `user_no_worse_than_best_disclosed=true` does not survive this check).
- `candidates_root_matches` — rebuild the candidates Merkle root.
- `within_limit`, `split_matches_agreed_policy` — `executedCost ≤ maxAmount`; the split equals
  `compute_split(surplus, agreed_policy)` exactly.
- `binding_digest_matches_nonce`, `binding_policy_hash_matches`, `binding_objective_matches`,
  `binding_max_amount_matches` — the disclosed binding commits to the agreed policy /
  objective / ceiling and its digest is the signed nonce.

What it does **not** check: that the settlement tx exists and moved value on chain (§7 step 9).

## 7. On-chain settlement (out of core — MUST be audited)

An on-chain `best-exec` requires a `BestExecSettler` contract (the EIP-3009 `to`). It is
**unbuilt** in this project and MUST be independently audited (gate H1). It MUST:

1. **Bind the nonce.** Reject unless the pulled authorization's `nonce == keccak256(binding)`
   for the binding whose fields it enforces — so `payTo`, `rebateTo`, `operatorTo`,
   `splitPolicyHash`, `objective`, `intentId`, window, and `chainId` are all cryptographically
   fixed to the user's signature.
2. **Pull exactly `maxAmount`** via `transferWithAuthorization`.
3. **Determine `executedCost` trustlessly** — execute the winning route itself (or verify an
   on-chain fill), never taking the facilitator's word for the price.
4. **Distribute atomically, no withholding** — `executedCost → payTo`, then the surplus per
   the bound `splitPolicy` (`user_rebate → rebateTo`, `operator_margin → operatorTo`, gas,
   referrer), summing exactly to `maxAmount`. The user's rebate MUST NOT be withholdable.
5. **Fall back** to the `exact` scheme (pay `maxAmount` to `payTo`) if no disclosed fill
   improves on the ceiling.

Only with such an audited settler, plus the mainnet gates H1–H3 in [ROADMAP](../ROADMAP.md),
is real-value `best-exec` appropriate.

## 8. Error reasons

`VALUE_NOT_MAX`, `OUTSIDE_WINDOW`, `POLICY_MISMATCH`, `MANDATE_REF_REQUIRED`,
`MANDATE_REF_MISMATCH`, `CROSS_CURRENCY_NOT_SUPPORTED`, `MAX_OUT_NOT_SUPPORTED_ONCHAIN`,
`BUDGET_EXCEEDED`, `DUPLICATE_INTENT`, `NO_WINNING_BID`, `GAS_EXCEEDS_SURPLUS`.

## 9. References

- [x402](https://github.com/coinbase/x402) · [whitepaper](https://www.x402.org/x402-whitepaper.pdf)
- [EIP-3009](https://eips.ethereum.org/EIPS/eip-3009) · [EIP-712](https://eips.ethereum.org/EIPS/eip-712)
- mandatehub: [`docs/EXECUTION_RECAPTURE.md`](../docs/EXECUTION_RECAPTURE.md) ·
  [`docs/X402.md`](../docs/X402.md) · implementation `mandatehub/x402/best_exec.py`
