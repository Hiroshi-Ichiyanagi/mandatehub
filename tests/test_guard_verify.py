"""guard-verify product: deterministic obol-guard decision as a machine-payable good.

All offline. The vendored guardcore must preserve the upstream semantics (canonical hard-DENY
order, soft-REVIEW collection); the product must refuse malformed input for FREE (precheck),
hash-pin every part of the request, and stay byte-stable for identical input.
"""
from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "local"


def _load(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    import products
    importlib.reload(products)
    return products


def _data(policy=None, candidate=None, state=None) -> str:
    cand = {"mandate_id": "m1", "payer": "0xA", "payee": "0xB",
            "amount_cents": 10_000, "at_ms": 1_700_000_000_000}
    if candidate:
        cand.update(candidate)
    return base64.b64encode(json.dumps(
        {"policy": policy or {}, "candidate": cand, "state": state or {}}).encode()).decode()


def test_vendored_guardcore_semantics(monkeypatch):
    """The flattened vendored core keeps upstream behavior: nonce-reuse outranks everything,
    hard beats soft, all triggered soft thresholds are reported."""
    monkeypatch.syspath_prepend(str(DEPLOY / "assets" / "obolguard"))
    import guardcore as G
    importlib.reload(G)

    # canonical hard order: NONCE_REUSED first even when other hard limits also violated
    d = G.evaluate(G.GuardPolicy(per_tx_max_cents=1),
                   G.Candidate(mandate_id="m", payer="a", payee="b", amount_cents=5, at_ms=1,
                               nonce="n"),
                   G.StateSnapshot(nonce_used=True))
    assert (d.verdict.value, d.reason) == ("DENY", "NONCE_REUSED")

    # hard beats soft: per-tx-max DENY wins over review thresholds
    d = G.evaluate(G.GuardPolicy(per_tx_max_cents=10, per_tx_review_cents=1),
                   G.Candidate(mandate_id="m", payer="a", payee="b", amount_cents=11, at_ms=1),
                   G.StateSnapshot())
    assert (d.verdict.value, d.reason) == ("DENY", "PER_TX_LIMIT_EXCEEDED")

    # soft thresholds collect ALL triggers
    d = G.evaluate(G.GuardPolicy(review_new_payee=True, per_tx_review_cents=1),
                   G.Candidate(mandate_id="m", payer="a", payee="b", amount_cents=5, at_ms=1),
                   G.StateSnapshot(seen_payee=False))
    assert d.verdict.value == "REVIEW"
    assert set(d.triggered) == {"NEW_PAYEE_REVIEW", "PER_TX_REVIEW"}

    # clean pass
    d = G.evaluate(G.GuardPolicy(), G.Candidate(mandate_id="m", payer="a", payee="b",
                                                amount_cents=1, at_ms=1), G.StateSnapshot())
    assert (d.verdict.value, d.reason, d.triggered) == ("ALLOW", "OK", ())


def test_guard_verify_product_paths(monkeypatch):
    """ALLOW / DENY / REVIEW through the product; response is hash-pinned and disclaimed."""
    P = _load(monkeypatch)

    allow = P.guard_verify({"data": _data()})
    assert allow["decision"] == "ALLOW" and allow["reason"] == "OK"
    assert "not payment advice" in allow["disclaimer"]

    deny = P.guard_verify({"data": _data(policy={"per_tx_max_cents": 100})})
    assert (deny["decision"], deny["reason"]) == ("DENY", "PER_TX_LIMIT_EXCEEDED")

    review = P.guard_verify({"data": _data(policy={"review_new_payee": True})})
    assert review["decision"] == "REVIEW" and review["triggered"] == ["NEW_PAYEE_REVIEW"]

    # allowlist round-trips through JSON lists -> frozenset
    ok = P.guard_verify({"data": _data(policy={"payee_allowlist": ["0xB"]})})
    assert ok["decision"] == "ALLOW"
    ng = P.guard_verify({"data": _data(policy={"payee_allowlist": ["0xC"]})})
    assert (ng["decision"], ng["reason"]) == ("DENY", "PAYEE_NOT_ALLOWED")

    # determinism: identical input -> byte-identical artifact hash
    a = P.guard_verify({"data": _data(policy={"daily_cap_cents": 500}, state={"spent_daily_cents": 100})})
    b = P.guard_verify({"data": _data(policy={"daily_cap_cents": 500}, state={"spent_daily_cents": 100})})
    assert a["artifact_sha256"] == b["artifact_sha256"]
    assert a["policy_sha256"] and a["candidate_sha256"] and a["state_sha256"]


def test_guard_verify_precheck_refuses_free(monkeypatch):
    """Malformed input — incl. an invalid POLICY — is refused pre-settlement (400, no charge)."""
    P = _load(monkeypatch)
    pre = P.guard_precheck
    assert pre({})[0] == 400                                            # missing data
    assert pre({"data": "!!!notb64"})[0] == 400                         # bad base64
    assert pre({"data": "A" * (P.GUARD_MAX_B64 + 1)})[0] == 400         # oversize
    assert pre({"data": base64.b64encode(b"[1,2]").decode()})[0] == 400  # not an object
    assert pre({"data": _data(policy={"bogus_field": 1})})[0] == 400     # unknown field
    code, body = pre({"data": base64.b64encode(json.dumps(
        {"policy": {}, "candidate": {"mandate_id": "m"}, "state": {}}).encode()).decode()})
    assert code == 400 and "missing required" in body["error"]
    # invalid policy VALUE (upstream PolicyError) is also a free refusal
    assert pre({"data": _data(policy={"per_tx_max_cents": -5})})[0] == 400
    # and a fully valid request passes precheck
    assert pre({"data": _data(policy={"per_tx_max_cents": 100000})}) is None


def test_guard_verify_registered_and_signed(monkeypatch, tmp_path):
    """Catalog registration + operator signature when the attest key is configured."""
    P = _load(monkeypatch)
    assert "guard-verify" in P.CATALOG
    assert P.CATALOG["guard-verify"].precheck is P.guard_precheck
    assert P.CATALOG["guard-verify"].available() is True   # vendored file ships with the repo

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception:
        import pytest
        pytest.skip("eth-account not installed")
    kf = tmp_path / "attest.key"
    kf.write_text("0x" + "44" * 32)
    monkeypatch.setenv("MANDATEHUB_ATTEST_KEY_FILE", str(kf))
    P._attest_cache.clear()
    out = P.guard_verify({"data": _data()})
    sig = out["operator_signature"]
    rec = Account.recover_message(encode_defunct(text=out["artifact_sha256"]),
                                  signature=sig["signature"])
    assert rec == sig["signer"] == P.attest_signer_address()
