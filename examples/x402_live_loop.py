"""P-live Step 3 — the full mandate-gated `402 → pay → settle → proof` loop, LIVE.

This is mandatehub actually *operating*: a resource server charges per API call over the
real x402 v1 wire (`X-PAYMENT` headers), a mandate (budget cap / purpose / replay) decides
whether the agent MAY pay, a REAL facilitator settles on-chain (Base Sepolia), and a
`ProofOfMandate` summary rides back with the response. The mandate gate runs BEFORE the
facilitator, so a replayed or over-budget payment is rejected for free — no network call,
no on-chain action (fail-closed).

The demo script drives four calls that spend REAL testnet USDC (default price 0.01 USDC):

  1. fresh payment  -> 200 (real on-chain settle #1)
  2. replayed X-PAYMENT -> 402 DUPLICATE_INTENT   (mandate blocks; facilitator never called)
  3. fresh payment  -> 200 (real on-chain settle #2)
  4. fresh payment  -> 402 BUDGET_EXCEEDED        (cap reached; facilitator never called)

Total real testnet spend: 2 x price (0.02 USDC by default).

Env (same as x402_live_smoke.py):
  MANDATEHUB_FACILITATOR_URL   e.g. https://x402.org/facilitator
  MANDATEHUB_AGENT_PRIVATE_KEY the paying agent's Base Sepolia key   (needs mandatehub[evm])
  MANDATEHUB_PAY_TO            the merchant's receiving address
  MANDATEHUB_AMOUNT            price per call in minor units (default 10000 = 0.01 USDC)

Network calls: only to the facilitator (verify/settle). NOT part of the offline suite.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    ProofOfMandateGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
)
from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    ExactEvmPayloadBuilder,
    RemoteFacilitatorAdapter,
    X402PaymentRequirements,
    decode_x_payment,
    encode_x_payment,
    encode_x_payment_response,
)

USDC = Currency.USDC


def _require(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"missing required env var: {key}")
    return v


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MandateGatedMerchant:
    """The resource server: real x402 v1 wire outside, mandate accounting inside."""

    def __init__(self, adapter: RemoteFacilitatorAdapter, requirements: X402PaymentRequirements,
                 budget_cap_cents: int, price_cents: int) -> None:
        self.adapter = adapter
        self.requirements = requirements
        self.price_cents = price_cents
        boot = _now()
        self.ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        self.audit = AuditLog(":memory:")
        plat = self.ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
        escrow = self.ledger.open_account(OwnerType.PLATFORM, USDC, "agent-escrow")
        b = TransactionBuilder("DEPOSIT", "ops", initiated_at=boot)
        b.transfer(plat.account_id, escrow.account_id, Money(budget_cap_cents, USDC))
        self.ledger.post(b.build())
        self.ledger.settle(b.transaction_id, settled_at=boot)
        self.merchant_account = self.ledger.open_account(OwnerType.USER, USDC, "merchant")
        self.engine = IntentSettlementEngine(self.ledger, audit_log=self.audit)
        self.engine.create_mandate(
            mandate_id="agent-m1", principal_id="hiroshi", escrow_account_id=escrow.account_id,
            budget_cap=Money(budget_cap_cents, USDC), allowed_purposes=frozenset(["API_CALL"]),
            valid_from=boot - timedelta(minutes=1), valid_until=boot + timedelta(days=1),
            created_at=boot,
        )

    def handle(self, x_payment: str | None) -> tuple[int, dict, dict[str, str]]:
        """One request. Returns (status, body, headers)."""
        if not x_payment:
            return 402, {"x402Version": 1, "error": "payment required",
                         "accepts": [self.requirements.to_wire()]}, {}

        payload = decode_x_payment(x_payment)
        auth = payload.payload.authorization
        intent_id = auth.nonce  # the EIP-3009 nonce doubles as a globally-unique intent id
        amount = Money(int(auth.value), USDC)
        at = _now()

        # 1) MANDATE GATE (free, offline, fail-closed) — before any facilitator call.
        ok, reason, _rem = self.engine.preauthorize(
            mandate_id="agent-m1", intent_id=intent_id,
            payee_account_id=self.merchant_account.account_id,
            amount=amount, purpose="API_CALL", at=at,
        )
        if not ok:
            return 402, {"x402Version": 1, "error": "rejected by mandate", "mandateReason": reason,
                         "accepts": [self.requirements.to_wire()]}, {}

        # 2) Facilitator verify (signature / funds, off-chain).
        v = self.adapter.verify(payload, self.requirements)
        if not v.is_valid:
            return 402, {"x402Version": 1, "error": "payment invalid",
                         "invalidReason": v.invalid_reason,
                         "accepts": [self.requirements.to_wire()]}, {}

        # 3) Facilitator settle — REAL on-chain transfer.
        s = self.adapter.settle(payload, self.requirements)
        if not s.success:
            return 402, {"x402Version": 1, "error": "settlement failed",
                         "errorReason": s.error_reason,
                         "accepts": [self.requirements.to_wire()]}, {}

        # 4) Record in the mandate ledger (budget plane) + proof.
        r = self.engine.settle_intent(
            mandate_id="agent-m1", intent_id=intent_id,
            payee_account_id=self.merchant_account.account_id,
            amount=amount, purpose="API_CALL", at=at,
        )
        proof, _tree = ProofOfMandateGenerator(self.engine).generate("agent-m1", snapshot_at=at)
        body = {
            "data": {"quote": "BTC/USD 68,000", "ts": at.isoformat()},
            "settlement": {"transaction": s.transaction, "network": s.network},
            "proofOfMandate": {
                "remaining_cents": proof.remaining_cents,
                "total_settled_cents": proof.total_settled_cents,
                "is_within_budget": proof.is_within_budget,
                "is_collateralized": proof.is_collateralized,
                "audit_log_root_hash": proof.audit_log_root_hash,
            },
        }
        assert r.decision == "SETTLED"
        return 200, body, {"X-PAYMENT-RESPONSE": encode_x_payment_response(s)}


def main() -> None:
    url = _require("MANDATEHUB_FACILITATOR_URL")
    private_key = _require("MANDATEHUB_AGENT_PRIVATE_KEY")
    pay_to = _require("MANDATEHUB_PAY_TO")
    price = int(os.environ.get("MANDATEHUB_AMOUNT", "10000"))       # 0.01 USDC
    budget = int(os.environ.get("MANDATEHUB_BUDGET", str(price * 2 + price // 2)))  # 2.5 calls

    try:
        from mandatehub.signers import EthAccountSigner
        signer = EthAccountSigner(private_key)
    except Exception as e:
        sys.exit(f"signer setup failed (pip install 'mandatehub[evm]'): {e}")

    requirements = X402PaymentRequirements(
        scheme="exact", network="base-sepolia", max_amount_required=str(price),
        asset=os.environ.get("MANDATEHUB_ASSET", BASE_SEPOLIA_USDC), pay_to=pay_to,
        resource="http://127.0.0.1:0/quote", max_timeout_seconds=60,
        description="one API call (mandate-gated live loop)",
        extra={"name": "USDC", "version": "2"},
    )
    # The merchant's SQLite objects must live in the thread that serves requests, so the
    # single-threaded HTTPServer AND the merchant are both created inside the server thread.
    boot: dict = {}
    ready = threading.Event()

    def run_server() -> None:
        merchant = MandateGatedMerchant(RemoteFacilitatorAdapter(url, network="base-sepolia"),
                                        requirements, budget_cap_cents=budget, price_cents=price)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                status, body, extra = merchant.handle(self.headers.get("X-PAYMENT"))
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # quiet
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        boot["server"] = server
        boot["port"] = server.server_address[1]
        ready.set()
        server.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()
    ready.wait(timeout=30)
    server = boot["server"]
    base = f"http://127.0.0.1:{boot['port']}/quote"
    print(f"merchant listening on {base}")
    print(f"price/call: {price / 1e6:.6f} USDC · mandate budget cap: {budget / 1e6:.6f} USDC")
    print(f"agent (payer): {signer.address}\nmerchant (payTo): {pay_to}\n")

    def call(headers: dict[str, str]) -> tuple[int, dict]:
        req = Request(base, headers=headers)
        try:
            with urlopen(req, timeout=90) as resp:
                return resp.status, json.loads(resp.read())
        except Exception as e:  # HTTPError has .read()
            if hasattr(e, "read"):
                return e.code, json.loads(e.read())  # type: ignore[attr-defined]
            raise

    def fresh_payment() -> str:
        status, body = call({})
        assert status == 402, f"expected 402 challenge, got {status}"
        reqs = X402PaymentRequirements.from_wire(body["accepts"][0])
        return encode_x_payment(ExactEvmPayloadBuilder(signer, network="base-sepolia").build(reqs))

    spent = 0
    # -- 1) fresh payment: the full 402 -> pay -> REAL settle -> 200 + proof ------------
    header1 = fresh_payment()
    status, body = call({"X-PAYMENT": header1})
    print(f"[1] fresh payment      -> {status}")
    assert status == 200, body
    print(f"    on-chain tx: {body['settlement']['transaction']}")
    print(f"    proof: remaining={body['proofOfMandate']['remaining_cents']} "
          f"within_budget={body['proofOfMandate']['is_within_budget']}")
    spent += price

    # -- 2) REPLAY the same X-PAYMENT: mandate must reject before any facilitator call --
    status, body = call({"X-PAYMENT": header1})
    print(f"[2] replayed payment   -> {status}  mandateReason={body.get('mandateReason')}")
    assert status == 402 and body.get("mandateReason") == "DUPLICATE_INTENT", body

    # -- 3) second fresh payment: budget still allows it --------------------------------
    status, body = call({"X-PAYMENT": fresh_payment()})
    print(f"[3] fresh payment      -> {status}")
    assert status == 200, body
    print(f"    on-chain tx: {body['settlement']['transaction']}")
    print(f"    proof: remaining={body['proofOfMandate']['remaining_cents']}")
    spent += price

    # -- 4) third fresh payment: cap (2.5x price) exceeded -> denied for free ----------
    status, body = call({"X-PAYMENT": fresh_payment()})
    print(f"[4] over-budget payment-> {status}  mandateReason={body.get('mandateReason')}")
    assert status == 402 and body.get("mandateReason") == "BUDGET_EXCEEDED", body

    server.shutdown()
    print(f"\nDONE — mandate-gated live loop complete. Real testnet USDC spent: {spent / 1e6:.6f}")
    print("Denied calls ([2] replay, [4] over-budget) never reached the facilitator: no network")
    print("call, no on-chain action — the mandate gate is fail-closed and free.")


if __name__ == "__main__":
    main()
