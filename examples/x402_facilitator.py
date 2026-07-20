"""A live x402 flow over real HTTP, gated by a mandatehub mandate.

Starts a real HTTP resource server on localhost that charges for a resource using the
HTTP 402 handshake (x402's way): the first request gets 402 + PAYMENT-REQUIRED; the client
signs a payment payload and retries with PAYMENT-SIGNATURE; the mandatehub facilitator
verifies the payment against the agent's mandate (budget / policy) and settles it, returning
the resource + a PAYMENT-RESPONSE carrying a ProofOfMandate. A replayed payment (same intent
id) is rejected by the mandate — over real HTTP.

Settlement here is the self-contained ledger adapter (no real money). Swapping in an
on-chain adapter (e.g. a real x402 facilitator on Base) is the only change needed to move
real value — see docs/X402.md.

The single-threaded HTTPServer's request loop runs in the main thread (same thread that owns
the SQLite ledger); the client runs in a background thread.

Run: python examples/x402_facilitator.py
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    TransactionBuilder,
)
from mandatehub.x402 import (
    Facilitator,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentPayload,
    PaymentRequirements,
    decode_requirements,
    decode_settle_response,
    encode_payload,
    serve_once,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def build_facilitator():
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
        budget_cap=usdc(100), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T, valid_until=T + timedelta(days=30), created_at=T, per_transaction_limit=usdc(40),
    )
    return Facilitator(eng), payee.account_id


FAC, PAYEE = build_facilitator()


def requirements_for(resource: str) -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact", network=FAC.network, max_amount_required_cents=usdc(30).cents,
        resource=resource, description="one API call", pay_to=PAYEE, asset="USDC",
        mandate_id="m1", purpose="API_CALL",
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        reqs = requirements_for(f"http://{self.headers.get('Host', 'localhost')}{self.path}")
        headers = {k: v for k, v in self.headers.items()}
        status, body, resp_headers = serve_once(
            FAC, reqs, headers, lambda: {"quote": "BTC/USD 68,000", "ts": "2026-01-01"}, at=T
        )
        payload = json.dumps(body).encode()
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence default logging
        pass


def run_client(url: str, httpd: HTTPServer) -> None:
    try:
        # 1) No payment -> 402 + PAYMENT-REQUIRED.
        print("--- client GET (no payment) ---")
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError as e:
            print(f"  <- {e.code} Payment Required")
            reqs = decode_requirements(e.headers[HEADER_PAYMENT_REQUIRED])
            print(f"     accepts {reqs.max_amount_required_cents/1e6:.2f} {reqs.asset} -> {reqs.pay_to[:8]}… (mandate {reqs.mandate_id}, purpose {reqs.purpose})")

        # 2) Sign a payment and retry -> 200 + resource + PAYMENT-RESPONSE (with proof).
        print("--- client GET (with payment, intent req-0001) ---")
        pay = encode_payload(PaymentPayload(scheme="exact", network=FAC.network, intent_id="req-0001", amount_cents=usdc(30).cents, payer="agent"))
        with urllib.request.urlopen(urllib.request.Request(url, headers={HEADER_PAYMENT_SIGNATURE: pay}), timeout=5) as resp:
            body = json.loads(resp.read().decode())
            settle = decode_settle_response(resp.headers[HEADER_PAYMENT_RESPONSE])
        print(f"  <- 200 OK  resource: {body}")
        p = settle["proof"]
        print(f"     settled tx {settle['transaction'][:16]}…  proof: within_budget={p['is_within_budget']} spent={p['total_settled_cents']/1e6:.2f} remaining={p['remaining_cents']/1e6:.2f} USDC")

        # 3) Replay the SAME payment -> mandate rejects it over HTTP (402 DUPLICATE_INTENT).
        print("--- client GET (replay same intent req-0001) ---")
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={HEADER_PAYMENT_SIGNATURE: pay}), timeout=5)
        except urllib.error.HTTPError as e:
            reason = json.loads(e.read().decode()).get("reason")
            print(f"  <- {e.code} rejected by mandate: {reason}")
    finally:
        httpd.shutdown()


def main() -> None:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)  # single-threaded: requests run in main thread
    url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/quote"
    threading.Thread(target=run_client, args=(url, httpd), daemon=True).start()
    httpd.serve_forever()  # main thread (same thread as the SQLite ledger)


if __name__ == "__main__":
    main()
