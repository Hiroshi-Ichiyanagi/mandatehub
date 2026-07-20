"""tests/test_x402.py — x402 互換ファシリテーター（verify/settle + 402 フロー）。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.intent import IntentSettlementEngine
from mandatehub.transparency.audit_log import AuditLog
from mandatehub.x402 import (
    Facilitator,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentPayload,
    PaymentRequirements,
    decode_payload,
    decode_requirements,
    decode_settle_response,
    encode_payload,
    serve_once,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


@pytest.fixture
def env():
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
        budget_cap=usdc(100), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T, valid_until=END, created_at=T, per_transaction_limit=usdc(40),
    )
    fac = Facilitator(eng)
    return {"ledger": ledger, "eng": eng, "payee": payee, "fac": fac}


def _reqs(env, amount=30):
    return PaymentRequirements(
        scheme="exact", network=env["fac"].network, max_amount_required_cents=usdc(amount).cents,
        resource="https://api.example/data", description="one API call",
        pay_to=env["payee"].account_id, asset="USDC", mandate_id="m1", purpose="API_CALL",
    )


def _payload(env, intent_id, amount, network=None):
    return PaymentPayload(
        scheme="exact", network=network or env["fac"].network, intent_id=intent_id,
        amount_cents=usdc(amount).cents, payer="agent",
    )


class TestVerify:
    def test_valid_is_non_mutating(self, env):
        before = sum(1 for _ in env["ledger"].iter_all_transactions())
        v = env["fac"].verify(_reqs(env), _payload(env, "i1", 30), at=T)
        assert v.is_valid and v.reason == "OK"
        assert sum(1 for _ in env["ledger"].iter_all_transactions()) == before  # /verify never posts

    def test_over_budget_invalid(self, env):
        # per-tx limit 40; ask 50 (also > max_required so AMOUNT_EXCEEDS_REQUIRED unless reqs raised)
        v = env["fac"].verify(_reqs(env, amount=60), _payload(env, "i1", 50), at=T)
        assert not v.is_valid and v.reason == "PER_TX_LIMIT_EXCEEDED"

    def test_amount_exceeds_required(self, env):
        v = env["fac"].verify(_reqs(env, amount=30), _payload(env, "i1", 35), at=T)
        assert not v.is_valid and v.reason == "AMOUNT_EXCEEDS_REQUIRED"

    def test_unsupported_network(self, env):
        v = env["fac"].verify(_reqs(env), _payload(env, "i1", 30, network="base-sepolia"), at=T)
        assert not v.is_valid and v.reason == "UNSUPPORTED_NETWORK"


class TestSettle:
    def test_settle_posts_and_proves(self, env):
        s = env["fac"].settle(_reqs(env), _payload(env, "i1", 30), at=T)
        assert s.success and s.reason == "OK"
        assert s.transaction  # a ledger tx id
        assert env["ledger"].balance(env["payee"].account_id, as_of=T) == usdc(30)
        assert s.proof is not None and s.proof["is_within_budget"] is True
        assert s.proof["total_settled_cents"] == usdc(30).cents

    def test_settle_denied_no_post(self, env):
        before = sum(1 for _ in env["ledger"].iter_all_transactions())
        s = env["fac"].settle(_reqs(env, amount=40), _payload(env, "i1", 40), at=T)  # ok
        assert s.success
        # second identical intent id -> duplicate -> denied, no new post
        mid = sum(1 for _ in env["ledger"].iter_all_transactions())
        s2 = env["fac"].settle(_reqs(env, amount=40), _payload(env, "i1", 40), at=T)
        assert not s2.success and s2.reason == "DUPLICATE_INTENT"
        assert sum(1 for _ in env["ledger"].iter_all_transactions()) == mid


class TestServeOnce:
    def test_402_then_200(self, env):
        reqs = _reqs(env)
        # no payment header -> 402 + PAYMENT-REQUIRED
        status, body, headers = serve_once(env["fac"], reqs, {}, lambda: {"data": 42}, at=T)
        assert status == 402
        assert HEADER_PAYMENT_REQUIRED in headers
        parsed = decode_requirements(headers[HEADER_PAYMENT_REQUIRED])
        assert parsed.max_amount_required_cents == usdc(30).cents and parsed.mandate_id == "m1"

        # retry with payment -> 200 + resource + PAYMENT-RESPONSE
        req_headers = {HEADER_PAYMENT_SIGNATURE: encode_payload(_payload(env, "i1", 30))}
        status2, body2, headers2 = serve_once(env["fac"], reqs, req_headers, lambda: {"data": 42}, at=T)
        assert status2 == 200 and body2 == {"data": 42}
        settle = decode_settle_response(headers2[HEADER_PAYMENT_RESPONSE])
        assert settle["success"] and settle["proof"]["is_within_budget"]

    def test_402_on_denied_payment(self, env):
        reqs = _reqs(env, amount=40)
        # ask 40 twice with same intent id: second is duplicate -> 402
        req_headers = {HEADER_PAYMENT_SIGNATURE: encode_payload(_payload(env, "dup", 40))}
        s1, _b, _h = serve_once(env["fac"], reqs, req_headers, lambda: {"ok": 1}, at=T)
        assert s1 == 200
        s2, body2, headers2 = serve_once(env["fac"], reqs, req_headers, lambda: {"ok": 1}, at=T)
        assert s2 == 402 and body2["reason"] == "DUPLICATE_INTENT"


class TestHeaderRoundTrip:
    def test_payload_roundtrip(self, env):
        p = _payload(env, "i9", 25)
        assert decode_payload(encode_payload(p)).intent_id == "i9"
        assert decode_payload(encode_payload(p)).amount_cents == usdc(25).cents
