"""tests/test_x402_best_exec.py — Phase 3: best-exec x402 scheme (offline accounting layer).

Stub verifier + in-ledger adapter — no network, no keys, no on-chain settler. Tests prove the
ACCOUNTING (best-of-disclosed, integer-exact split, self-consistent proofs) and the
nonce-binding logic; the real BestExecSettler contract + keccak256 + real signatures are
out-of-core (see best_exec.py docstring / docs/X402.md).
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub import (
    AuditLog,
    Currency,
    ExecutionAccounts,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    SolverBid,
    SurplusSplitPolicy,
    TransactionBuilder,
)
from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    BestExecFacilitator,
    BestExecParams,
    X402PaymentRequirements,
    binding_digest,
    build_best_exec_payload,
    split_policy_hash,
    verify_best_exec_response,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)
POL = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _env(cap=1000, objective="MIN_COST", currency_out="USDC", gas_cents=0, mandate_ref="m1"):
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(cap))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "venue")
    rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "rebate")
    margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "margin")
    gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
                       budget_cap=usdc(cap), allowed_purposes=frozenset(["X402_BEST_EXEC"]),
                       valid_from=T, valid_until=END, created_at=T)
    accts = ExecutionAccounts(payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id,
                              operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id)
    pol = SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000, gas_reimbursement_cents=gas_cents)
    be = BestExecParams(objective=objective, settler="0xBestExecSettler", split_policy=pol,
                        rebate_to=rebate.account_id, operator_to=margin.account_id,
                        currency_out=currency_out, mandate_ref=mandate_ref, purpose="X402_BEST_EXEC")
    reqs = X402PaymentRequirements(scheme="best-exec", network="base-sepolia", max_amount_required=str(usdc(100).cents),
                                   asset=BASE_SEPOLIA_USDC, pay_to=payee.account_id, resource="https://api/execute",
                                   max_timeout_seconds=60, extra={"name": "USDC", "version": "2", "bestExec": be.to_wire()})
    fac = BestExecFacilitator(eng, "m1", accts, network="base-sepolia")
    bids = [SolverBid("sA", "i1", usdc(90).cents, 0, 0), SolverBid("sB", "i1", usdc(95).cents, 0, 0)]
    return dict(ledger=ledger, eng=eng, accts=accts, reqs=reqs, fac=fac, bids=bids, pol=pol,
                payee=payee, rebate=rebate)


def _payload(env, intent_id="i1"):
    return build_best_exec_payload(env["reqs"], from_addr="0xUser", at=T, intent_id=intent_id)


class TestWire:
    def test_params_roundtrip(self):
        be = BestExecParams(objective="MIN_COST", settler="0xS", split_policy=POL, rebate_to="0xR", operator_to="0xO")
        assert BestExecParams.from_wire(be.to_wire()).split_policy == POL

    def test_bad_policy_rejected(self):
        with pytest.raises(Exception):
            SurplusSplitPolicy(user_rebate_bps=6000, operator_margin_bps=3000)  # sums 9000


class TestVerify:
    def test_accept(self):
        env = _env()
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T) == (True, "OK")

    def test_policy_mismatch_on_tampered_nonce(self):
        env = _env()
        p = _payload(env)
        bad = dataclasses.replace(p, authorization=dataclasses.replace(p.authorization, nonce="0xdead"))
        assert env["fac"].verify(env["reqs"], bad, env["bids"], at=T) == (False, "POLICY_MISMATCH")

    def test_policy_mismatch_on_tampered_binding_field(self):
        env = _env()
        p = _payload(env)
        tampered_binding = dict(p.binding, payTo="0xATTACKER")  # digest no longer matches nonce
        bad = dataclasses.replace(p, binding=tampered_binding)
        assert env["fac"].verify(env["reqs"], bad, env["bids"], at=T)[1] == "POLICY_MISMATCH"

    def test_value_not_max(self):
        env = _env()
        p = _payload(env)
        bad = dataclasses.replace(p, authorization=dataclasses.replace(p.authorization, value="123"))
        assert env["fac"].verify(env["reqs"], bad, env["bids"], at=T) == (False, "VALUE_NOT_MAX")

    def test_outside_window(self):
        env = _env()
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T + timedelta(seconds=120))[1] == "OUTSIDE_WINDOW"

    def test_no_winning_bid(self):
        env = _env()
        invalid_bids = [SolverBid("sA", "i1", usdc(90).cents, 0, 0, valid=False)]
        assert env["fac"].verify(env["reqs"], _payload(env), invalid_bids, at=T) == (False, "NO_WINNING_BID")

    def test_gas_exceeds_surplus(self):
        env = _env(gas_cents=usdc(50).cents)  # surplus is 10 USDC < 50 gas
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T) == (False, "GAS_EXCEEDS_SURPLUS")

    def test_mandate_over_budget(self):
        env = _env(cap=100)
        env["fac"].settle(env["reqs"], _payload(env, "i1"), env["bids"], at=T)  # consumes 100
        assert env["fac"].verify(env["reqs"], _payload(env, "i2"), env["bids"], at=T)[1] == "BUDGET_EXCEEDED"

    def test_onchain_maxout_and_crosscurrency_guards(self):
        env = _env(objective="MAX_OUT")
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T, on_chain=True)[1] == "MAX_OUT_NOT_SUPPORTED_ONCHAIN"
        env2 = _env(currency_out="JPY")
        assert env2["fac"].verify(env2["reqs"], _payload(env2), env2["bids"], at=T, on_chain=True)[1] == "CROSS_CURRENCY_NOT_SUPPORTED"

    def test_mandate_ref_required(self):
        env = _env(mandate_ref=None)
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T) == (False, "MANDATE_REF_REQUIRED")

    def test_mandate_ref_mismatch(self):
        env = _env(mandate_ref="m2")  # facilitator is bound to "m1"
        assert env["fac"].verify(env["reqs"], _payload(env), env["bids"], at=T) == (False, "MANDATE_REF_MISMATCH")

    def test_policy_mismatch_when_attacker_self_consistently_rebinds(self):
        # The digest-only check is not enough: an attacker can tamper a bound field AND
        # recompute a matching nonce. The field-level cross-check must catch each one.
        env = _env()
        p = _payload(env)
        for fld, value in [
            ("operatorTo", "0xATTACKER"),   # fee redirect
            ("payTo", "0xATTACKER"),        # fund redirect
            ("rebateTo", "0xATTACKER"),     # rebate theft
            ("settler", "0xEVIL"),          # swap the settler contract
            ("asset", "0xEVIL"),            # swap the token
            ("splitPolicyHash", "0xffff"),  # fee grab
            ("objective", "MAX_OUT"),       # change the auction objective
            ("maxAmount", "1"),             # loosen the ceiling
            ("chainId", 1),                 # cross-chain replay
            ("validBefore", "9999999999"),  # widen the window vs the signed authorization
        ]:
            bad_binding = dict(p.binding, **{fld: value})
            bad = dataclasses.replace(
                p,
                binding=bad_binding,
                authorization=dataclasses.replace(p.authorization, nonce=binding_digest(bad_binding)),
            )
            assert env["fac"].verify(env["reqs"], bad, env["bids"], at=T) == (False, "POLICY_MISMATCH"), fld


class TestSettleAndOfflineVerify:
    def test_happy_path_and_offline_reverify(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        assert res.success
        assert int(res.response["executedCost"]) == usdc(90).cents
        assert res.response["split"]["user_rebate"] == str(usdc(7).cents)
        # ledger reflects it: payee got executed cost, user got the rebate
        assert env["ledger"].balance(env["payee"].account_id, as_of=T) == usdc(90)
        assert env["ledger"].balance(env["rebate"].account_id, as_of=T) == usdc(7)
        # a third party re-verifies the accounting from the response alone
        chk = verify_best_exec_response(res.response, agreed_policy=env["pol"])
        assert all(chk.values()), {k: v for k, v in chk.items() if not v}

    def test_offline_catches_executed_cost_inflation(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        tampered = dict(res.response, executedCost=str(usdc(93).cents))  # claim a worse fill than winner (90)
        chk = verify_best_exec_response(tampered, agreed_policy=env["pol"])
        assert chk["executed_cost_matches_winner"] is False

    def test_offline_catches_wrong_split(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        bad = dict(res.response)
        bad["split"] = dict(bad["split"], user_rebate=str(usdc(9).cents))  # not what the policy yields
        chk = verify_best_exec_response(bad, agreed_policy=env["pol"])
        assert chk["split_matches_agreed_policy"] is False

    def test_candidates_root_deterministic_across_independent_runs(self):
        r1 = _env()["fac"].settle(_env()["reqs"], None, None, at=T) if False else None
        # two independent engines, same bids/objective -> same candidatesMerkleRoot (over solver ids, not accounts)
        e1, e2 = _env(), _env()
        a = e1["fac"].settle(e1["reqs"], _payload(e1), e1["bids"], at=T).response
        b = e2["fac"].settle(e2["reqs"], _payload(e2), e2["bids"], at=T).response
        assert a["auction"]["candidatesMerkleRoot"] == b["auction"]["candidatesMerkleRoot"]
        assert a["auction"]["winnerId"] == b["auction"]["winnerId"] == "sA"

    def test_replay_same_intent_denied(self):
        env = _env()
        env["fac"].settle(env["reqs"], _payload(env, "i1"), env["bids"], at=T)
        res2 = env["fac"].settle(env["reqs"], _payload(env, "i1"), env["bids"], at=T)
        assert not res2.success and res2.reason == "DUPLICATE_INTENT"

    def test_settlement_plane_is_honest(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        # the in-ledger facilitator must never claim the funds moved on chain
        assert res.response["settlementPlane"] == "in-ledger"
        assert res.response["txHash"].startswith("0xLEDGER:")

    def test_offline_independent_no_worse_is_not_circular(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        # attacker inflates the executed cost AND forges the proof's self-reported flag to True
        tampered = dict(res.response, executedCost=str(usdc(93).cents))
        tampered["proofOfBestExecution"] = dict(
            res.response["proofOfBestExecution"], user_no_worse_than_best_disclosed=True
        )
        chk = verify_best_exec_response(tampered, agreed_policy=env["pol"])
        assert chk["proof_no_worse_than_best_disclosed"] is True       # forged flag believed as-is...
        assert chk["independent_no_worse_than_disclosed"] is False     # ...but the recompute catches it

    def test_offline_verifies_binding_commitment(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        assert "binding" in res.response and "nonce" in res.response
        chk = verify_best_exec_response(res.response, agreed_policy=env["pol"])
        assert chk["binding_digest_matches_nonce"] is True
        assert chk["binding_policy_hash_matches"] is True
        assert chk["binding_objective_matches"] is True
        assert chk["binding_max_amount_matches"] is True

    def test_offline_catches_binding_tamper(self):
        env = _env()
        res = env["fac"].settle(env["reqs"], _payload(env), env["bids"], at=T)
        # tamper the disclosed binding's policy hash without fixing the committed nonce
        bad = dict(res.response, binding=dict(res.response["binding"], splitPolicyHash="0xffff"))
        chk = verify_best_exec_response(bad, agreed_policy=env["pol"])
        assert chk["binding_digest_matches_nonce"] is False
        assert chk["binding_policy_hash_matches"] is False
