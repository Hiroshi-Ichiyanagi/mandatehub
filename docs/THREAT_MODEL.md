# Threat model & audit scope (H1 preparation)

What an independent security review of mandatehub should attack, what the system already
claims to defend (each claim mapped to enforcing code + tests), and what is **known to be
out of scope or unbuilt**. Honest by construction: the "known gaps" section is as load-bearing
as the defenses.

## 1. Assets

| asset | where | why it matters |
| ----- | ----- | -------------- |
| The mandate's budget (escrow) | ledger (SQLite, append-only) | overspend = the core failure |
| Settlement history (replay state) | ledger `INTENT_SETTLEMENT` txs | replay = double-spend of an authorization |
| Audit chain | `audit.db` hash chain | tamper = forged lifecycle/proof history |
| Proof artifacts (`ProofOfMandate`, best-exec proofs) | generated offline | forged proof = false assurance to a verifier |
| Agent private key (EIP-3009 signer) | **never in this core**; operator env / wallet | theft = arbitrary spend up to on-chain balance |
| EIP-3009 authorization (signed payload) | x402 wire | mis-binding = funds repointed |

## 2. Adversaries

1. **Malicious/compromised agent** — tries to spend beyond its mandate (over budget, wrong
   purpose, replay, backdated timestamps, nonce games, sibling-session collusion).
2. **Malicious operator/facilitator** — tries to skim surplus, repoint funds, forge proofs,
   or under-report fills (best-exec).
3. **Third-party attacker on the wire** — replays or tampers `X-PAYMENT` / responses.
4. **Post-hoc forger** — tries to rewrite history after settlement (ledger/audit tamper).

## 3. Defended claims (attack these first)

Each row: the claim, the enforcing code, and the test that would fail if it broke.

| claim | enforced in | tested by |
| ----- | ----------- | --------- |
| Budget can never be exceeded (incl. whole delegation subtree) | `_authorize` checks 21–22 (`mandate.py`) | intent tests; `tests/test_rehydration.py` |
| Replay is impossible (intent id + nonce, across restarts) | checks 13–15; storage-layer re-derivation | `test_rehydration.py::test_budget_replay…` |
| Backdating can't evade epoch/velocity caps | full-ledger read + `NON_MONOTONIC_TIME` (check 7) | monotonic-time tests |
| Session keys can't leak past any ancestor's cap | check 21 recomputes every ancestor's subtree | sub-mandate non-leakage tests |
| A denied settlement moves no money | deny path posts nothing (`settle_intent`) | denial tests (tx-count delta 0) |
| Ledger always balances to zero per currency | `TransactionBuilder`/`Ledger.post` | core ledger tests |
| Surplus split is integer-exact; a leak is un-postable | `compute_split` + balanced tx | execution tests |
| INV-9: best-exec is budget-side byte-identical to plain | `bridge.py` two-plane posting | INV-9 tests |
| Proofs are deterministic (state, time) → byte-identical | explicit-time discipline; as-of audit root | determinism guard tests (runtime + AST) |
| best-exec binding can't be repointed without breaking the signature | nonce = digest(binding); verify cross-checks **every** field | `test_policy_mismatch_when_attacker_self_consistently_rebinds` |
| Client wire is hardened | https-only, no cross-host redirect, fail-closed on non-2xx/malformed, real UA | `tests/test_x402_remote.py` (23 tests) |
| Restart survival (H2) | `rehydrate_mandate` + file-backed SQLite | `test_rehydration.py` (5 tests) + live SIGKILL test 2026-07-21 |

## 4. Known gaps — the audit should confirm these are real and bounded

1. **Single-writer assumption.** In-process serialization + private SQLite files. Two
   processes on the same files, or any multi-worker deployment, voids the replay/budget
   guarantees until the constraints move into a shared store (Postgres/D1). *(H2 remainder.)*
2. **No auth / rate limiting on the operator HTTP surface.** The reference operator binds
   127.0.0.1 and expects a tunnel in front; it has no API auth of its own.
3. **`BestExecSettler` contract is unbuilt.** On-chain atomic split / no-withholding / nonce
   enforcement for best-exec is specified (`specs/best-exec.md` §7) but does not exist; the
   offline layer says so (`settlementPlane:"in-ledger"`).
4. **Two hash domains.** Offline binding commitments are sha256 (stdlib); on-chain nonces are
   keccak256. Verified logically equivalent over the same preimage, but an auditor should
   confirm no cross-domain confusion is exploitable.
5. **Facilitator trust.** `/settle` success is taken from the facilitator's response; the
   operator does not independently confirm the on-chain tx (one RPC read would close this —
   listed as hardening backlog).
6. **Best-exec honesty bounds.** "Best of the *disclosed* candidates" only; a suppressed bid
   or fully-colluding solver pool is out of offline scope (documented in the spec).
7. **Key handling is out of core.** `EthAccountSigner` takes a raw key from the caller;
   KMS/HSM integration is deployment-side. The operator process never holds the agent key.
8. **Denial `remaining` placeholder.** Checks 1–6 return `remaining=0` rather than the true
   remaining (cosmetic, but an integrator could misread it).

## 5. Suggested audit scope (priced small → large)

- **S — wire + client (1–2 days):** `x402/wire.py`, `remote.py`, `exact_evm.py`, `eip712.py`
  against the x402 v1 spec + EIP-3009/712; header codecs; UA/redirect/TLS behavior.
- **M — mandate core (3–5 days):** `_authorize` (all 22 checks, ordering, full-ledger read),
  settlement record extraction (fail-closed), rehydration, lifecycle fold, proofs +
  Merkle/audit-chain commitments. Adversarial focus: backdating, nonce/id collisions,
  sibling collusion, escrow co-tenancy.
- **L — best-exec (1 week):** binding/nonce commitment, verify matrix completeness,
  `verify_best_exec_response` independence (no trust in self-reported flags), split
  exactness, Model B cross-currency accounting; §7 settler MUSTs as a design review.

## 6. Reproduction environment

Everything is reproducible with `pip install -e ".[test]" && python -m pytest -q`
(229 tests, stdlib-only runtime) plus the runnable examples; the live path is
re-runnable via [`docs/TESTNET.md`](TESTNET.md) with faucet funds only.
