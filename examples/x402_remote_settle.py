"""Phase 2: settle an x402 `exact` payment via a real facilitator client (against a stub).

Builds a real x402 v1 exact/EVM payment payload (EIP-3009 + EIP-712), then calls a
facilitator's /verify and /settle over HTTP using RemoteFacilitatorAdapter. Here the
facilitator is an in-process STUB and the signer is a StubSigner, so this runs with no
network and no keys — but the wire format, headers, and request/response are exactly what a
real x402 v1 facilitator (e.g. Coinbase CDP on Base Sepolia) speaks.

To go real (operator's last mile), change two things and nothing else:
    from mandatehub.signers import EthAccountSigner            # pip install 'mandatehub[evm]'
    signer  = EthAccountSigner(os.environ["MANDATEHUB_AGENT_PRIVATE_KEY"])
    adapter = RemoteFacilitatorAdapter(os.environ["MANDATEHUB_FACILITATOR_URL"])  # https://…

Run: python examples/x402_remote_settle.py
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    ExactEvmPayloadBuilder,
    RemoteFacilitatorAdapter,
    StubSigner,
    X402PaymentRequirements,
    encode_x_payment,
)


class StubFacilitator(BaseHTTPRequestHandler):
    """A canned x402 v1 facilitator: asserts the request envelope, returns success."""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        assert body["x402Version"] == 1 and "paymentPayload" in body and "paymentRequirements" in body
        payer = body["paymentPayload"]["payload"]["authorization"]["from"]
        if self.path.endswith("/verify"):
            resp = {"isValid": True, "invalidReason": None, "payer": payer}
        elif self.path.endswith("/settle"):
            resp = {"success": True, "errorReason": None, "payer": payer,
                    "transaction": "0xSIMULATED_ONCHAIN_TX_HASH", "network": "base-sepolia"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main() -> None:
    httpd = HTTPServer(("127.0.0.1", 0), StubFacilitator)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # The resource server's 402 told us to pay 0.01 USDC (10000 atomic units) to payTo.
        reqs = X402PaymentRequirements(
            scheme="exact", network="base-sepolia", max_amount_required="10000",
            asset=BASE_SEPOLIA_USDC, pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            resource="https://api.example/v1/quote", max_timeout_seconds=60,
            extra={"name": "USDC", "version": "2"},
        )
        # The agent signs an EIP-3009 transferWithAuthorization (StubSigner here; EthAccountSigner in prod).
        payload = ExactEvmPayloadBuilder(StubSigner(), network="base-sepolia").build(reqs)
        print("--- x402 exact/EVM payment payload ---")
        auth = payload.payload.authorization
        print(f"  from {auth.from_[:12]}…  to {auth.to[:12]}…  value {auth.value} (0.01 USDC)")
        print(f"  validAfter {auth.valid_after}  validBefore {auth.valid_before}  nonce {auth.nonce[:14]}…")
        print(f"  X-PAYMENT header (base64, {len(encode_x_payment(payload))} chars): {encode_x_payment(payload)[:40]}…")

        # Talk to the facilitator (stub here; a real https facilitator in prod).
        adapter = RemoteFacilitatorAdapter(f"http://127.0.0.1:{port}/facilitator")
        print("\n--- facilitator /verify ---")
        v = adapter.verify(payload, reqs)
        print(f"  isValid={v.is_valid}  payer={v.payer[:12]}…")
        print("--- facilitator /settle ---")
        s = adapter.settle(payload, reqs)
        print(f"  success={s.success}  tx={s.transaction}  network={s.network}")
        print("\n(stub settlement — no real value moved. Set MANDATEHUB_FACILITATOR_URL + EthAccountSigner for real Base Sepolia.)")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
