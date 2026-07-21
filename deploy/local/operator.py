"""mandatehub operator — the durable, restart-safe, mandate-gated x402 resource server.

This is the H2 ("production hardening") tier of the live loop
(examples/x402_live_loop.py): same real x402 v1 wire + mandate gate + real facilitator
settlement, but built to RUN — file-backed SQLite ledger + audit chain, mandate
rehydration on boot, /healthz, logging, and fail-closed behavior on every path. Pair it
with the launchd plist in this directory to keep it resident (the x402-Gateway pattern).

State lives under MANDATEHUB_DATA_DIR (default ~/.mandatehub-operator):
  ledger.db     append-only double-entry ledger      (SQLite)
  audit.db      hash-chained audit log               (SQLite)
  mandate.json  the mandate's parameters — on boot, if present, the mandate is
                REHYDRATED (no new audit event, no double history); if absent, it is
                created once and the config written.

Restart guarantee (tested in tests/test_rehydration.py): budget spent, replay
(intent/nonce), monotonic time, and lifecycle all survive a process restart because they
are re-derived from storage — never from process memory.

Env:
  MANDATEHUB_FACILITATOR_URL  (required)  e.g. https://x402.org/facilitator
  MANDATEHUB_PAY_TO           (required)  merchant receiving address
  MANDATEHUB_PORT             default 8402
  MANDATEHUB_DATA_DIR         default ~/.mandatehub-operator
  MANDATEHUB_AMOUNT           price per call, minor units (default 10000 = 0.01 USDC)
  MANDATEHUB_BUDGET           mandate budget cap, minor units (default 1000000 = 1 USDC)
  MANDATEHUB_NETWORK          default base-sepolia

Concurrency note (OPERATIONS.md "multi-worker rule"): this process is single-threaded on
purpose — the SQLite storage layer is the final line of defense, and in-process
serialization is only trusted because there is exactly one process. Scaling out requires
moving the uniqueness/atomicity constraints into a shared store (Postgres/D1) first.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Mandate,
    Money,
    OwnerType,
    ProofOfMandateGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
)
from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    RemoteFacilitatorAdapter,
    X402PaymentRequirements,
    decode_x_payment,
    encode_x_payment_response,
)

USDC = Currency.USDC
log = logging.getLogger("mandatehub.operator")


def _require(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"missing required env var: {key}")
    return v


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Operator:
    def __init__(self, data_dir: Path, *, adapter, requirements: X402PaymentRequirements,
                 budget_cents: int) -> None:
        self.adapter = adapter
        self.requirements = requirements
        data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(SQLiteLedgerStorage(str(data_dir / "ledger.db")))
        self.audit = AuditLog(str(data_dir / "audit.db"))
        self.engine = IntentSettlementEngine(self.ledger, audit_log=self.audit)
        self.settled_count = 0
        self.denied_count = 0

        cfg_path = data_dir / "mandate.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            mandate = Mandate(
                mandate_id=cfg["mandate_id"], principal_id=cfg["principal_id"],
                escrow_account_id=cfg["escrow_account_id"], currency=USDC,
                budget_cap=Money(cfg["budget_cap_cents"], USDC),
                allowed_purposes=frozenset(cfg["allowed_purposes"]),
                valid_from=datetime.fromisoformat(cfg["valid_from"]),
                valid_until=datetime.fromisoformat(cfg["valid_until"]),
                created_at=datetime.fromisoformat(cfg["created_at"]),
            )
            self.engine.rehydrate_mandate(mandate)
            self.merchant_account_id = cfg["merchant_account_id"]
            log.info("rehydrated mandate %s (budget %s cents) from %s",
                     mandate.mandate_id, cfg["budget_cap_cents"], cfg_path)
        else:
            boot = _now()
            plat = self.ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
            escrow = self.ledger.open_account(OwnerType.PLATFORM, USDC, "agent-escrow")
            b = TransactionBuilder("DEPOSIT", "ops", initiated_at=boot)
            b.transfer(plat.account_id, escrow.account_id, Money(budget_cents, USDC))
            self.ledger.post(b.build())
            self.ledger.settle(b.transaction_id, settled_at=boot)
            merchant = self.ledger.open_account(OwnerType.USER, USDC, "merchant")
            cfg = dict(
                mandate_id="operator-m1", principal_id="operator",
                escrow_account_id=escrow.account_id, budget_cap_cents=budget_cents,
                allowed_purposes=["API_CALL"],
                valid_from=(boot - timedelta(minutes=1)).isoformat(),
                valid_until=(boot + timedelta(days=365)).isoformat(),
                created_at=boot.isoformat(), merchant_account_id=merchant.account_id,
            )
            self.engine.create_mandate(
                mandate_id=cfg["mandate_id"], principal_id=cfg["principal_id"],
                escrow_account_id=escrow.account_id, budget_cap=Money(budget_cents, USDC),
                allowed_purposes=frozenset(["API_CALL"]),
                valid_from=boot - timedelta(minutes=1), valid_until=boot + timedelta(days=365),
                created_at=boot,
            )
            self.merchant_account_id = merchant.account_id
            cfg_path.write_text(json.dumps(cfg, indent=2))
            log.info("created mandate %s (budget %s cents); config -> %s",
                     cfg["mandate_id"], budget_cents, cfg_path)
        self.mandate_id = "operator-m1"

    # -- request handling -----------------------------------------------------------
    def health(self) -> dict:
        at = _now()
        return {
            "ok": True,
            "mandate": self.mandate_id,
            "remaining_cents": self.engine.remaining_cents(self.mandate_id, as_of=at),
            "settled_this_process": self.settled_count,
            "denied_this_process": self.denied_count,
            "audit_root": self.audit.latest_hash(),
        }

    def handle_payment(self, x_payment: str | None) -> tuple[int, dict, dict[str, str]]:
        if not x_payment:
            return 402, {"x402Version": 1, "error": "payment required",
                         "accepts": [self.requirements.to_wire()]}, {}
        try:
            payload = decode_x_payment(x_payment)
        except Exception:
            self.denied_count += 1
            return 402, {"x402Version": 1, "error": "malformed X-PAYMENT",
                         "accepts": [self.requirements.to_wire()]}, {}
        auth = payload.payload.authorization
        at = _now()
        ok, reason, _ = self.engine.preauthorize(
            mandate_id=self.mandate_id, intent_id=auth.nonce,
            payee_account_id=self.merchant_account_id,
            amount=Money(int(auth.value), USDC), purpose="API_CALL", at=at)
        if not ok:
            self.denied_count += 1
            log.info("DENY %s (mandate gate; facilitator not called)", reason)
            return 402, {"x402Version": 1, "error": "rejected by mandate",
                         "mandateReason": reason,
                         "accepts": [self.requirements.to_wire()]}, {}
        v = self.adapter.verify(payload, self.requirements)
        if not v.is_valid:
            self.denied_count += 1
            log.info("DENY facilitator verify: %s", v.invalid_reason)
            return 402, {"x402Version": 1, "error": "payment invalid",
                         "invalidReason": v.invalid_reason,
                         "accepts": [self.requirements.to_wire()]}, {}
        s = self.adapter.settle(payload, self.requirements)
        if not s.success:
            self.denied_count += 1
            log.warning("DENY facilitator settle: %s", s.error_reason)
            return 402, {"x402Version": 1, "error": "settlement failed",
                         "errorReason": s.error_reason,
                         "accepts": [self.requirements.to_wire()]}, {}
        r = self.engine.settle_intent(
            mandate_id=self.mandate_id, intent_id=auth.nonce,
            payee_account_id=self.merchant_account_id,
            amount=Money(int(auth.value), USDC), purpose="API_CALL", at=at)
        assert r.decision == "SETTLED"  # preauthorize passed; same inputs
        proof, _ = ProofOfMandateGenerator(self.engine).generate(self.mandate_id, snapshot_at=at)
        self.settled_count += 1
        log.info("SETTLED %s on-chain tx=%s remaining=%s", auth.nonce[:18], s.transaction,
                 proof.remaining_cents)
        return 200, {
            "data": {"quote": "BTC/USD 68,000", "ts": at.isoformat()},
            "settlement": {"transaction": s.transaction, "network": s.network},
            "proofOfMandate": {
                "remaining_cents": proof.remaining_cents,
                "total_settled_cents": proof.total_settled_cents,
                "is_within_budget": proof.is_within_budget,
                "is_collateralized": proof.is_collateralized,
                "audit_log_root_hash": proof.audit_log_root_hash,
            },
        }, {"X-PAYMENT-RESPONSE": encode_x_payment_response(s)}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = _require("MANDATEHUB_FACILITATOR_URL")
    pay_to = _require("MANDATEHUB_PAY_TO")
    port = int(os.environ.get("MANDATEHUB_PORT", "8402"))
    data_dir = Path(os.environ.get("MANDATEHUB_DATA_DIR",
                                   str(Path.home() / ".mandatehub-operator")))
    price = os.environ.get("MANDATEHUB_AMOUNT", "10000")
    budget = int(os.environ.get("MANDATEHUB_BUDGET", "1000000"))
    network = os.environ.get("MANDATEHUB_NETWORK", "base-sepolia")

    requirements = X402PaymentRequirements(
        scheme="exact", network=network, max_amount_required=price,
        asset=os.environ.get("MANDATEHUB_ASSET", BASE_SEPOLIA_USDC), pay_to=pay_to,
        resource=f"http://127.0.0.1:{port}/quote", max_timeout_seconds=60,
        description="one API call (mandatehub operator)",
        extra={"name": "USDC", "version": "2"},
    )
    op = Operator(data_dir, adapter=RemoteFacilitatorAdapter(url, network=network),
                  requirements=requirements, budget_cents=budget)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                status, body, extra = 200, op.health(), {}
            else:
                status, body, extra = op.handle_payment(self.headers.get("X-PAYMENT"))
            data = json.dumps(body).encode()
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    log.info("mandatehub operator listening on http://127.0.0.1:%s (data: %s)", port, data_dir)
    server.serve_forever()


if __name__ == "__main__":
    main()
