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
  MANDATEHUB_PORT             default 8403
  MANDATEHUB_DATA_DIR         default ~/.mandatehub-operator
  MANDATEHUB_AMOUNT           price per call, minor units (default 10000 = 0.01 USDC)
  MANDATEHUB_BUDGET           mandate budget cap, minor units (default 1000000 = 1 USDC)
  MANDATEHUB_NETWORK          default base-sepolia
  MANDATEHUB_RATE_PER_MIN     optional native rate limit (max settlements / 60s)
  MANDATEHUB_DB_URL           optional Postgres conninfo for a SHARED ledger
                              (multi-worker; needs pip install 'mandatehub[postgres]')

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
    SpendPolicy,
    SQLiteLedgerStorage,
    TransactionBuilder,
)
from mandatehub.x402 import (
    BASE_MAINNET_USDC,
    BASE_MAINNET_USDC_DOMAIN,
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
                 budget_cents: int, rate_per_min: int | None = None,
                 db_url: str | None = None) -> None:
        self.adapter = adapter
        self.requirements = requirements
        data_dir.mkdir(parents=True, exist_ok=True)
        if db_url:
            # Shared Postgres ledger → the atomic unique-PK claim makes concurrent replay
            # impossible across workers (docs/MULTIWORKER.md). NOTE: the audit log is still
            # local SQLite here — running >1 worker also needs a shared audit store; the
            # money-path safety (no double-spend) comes from the shared ledger + claim.
            from mandatehub.storage_postgres import PostgresLedgerStorage
            self.ledger = Ledger(PostgresLedgerStorage(db_url))
            log.info("ledger backend: Postgres (multi-worker capable)")
        else:
            self.ledger = Ledger(SQLiteLedgerStorage(str(data_dir / "ledger.db")))
        self.audit = AuditLog(str(data_dir / "audit.db"))
        self.engine = IntentSettlementEngine(self.ledger, audit_log=self.audit)
        self.settled_count = 0
        self.denied_count = 0

        def _policy(cfg: dict) -> "SpendPolicy | None":
            # Native rate limiting via a rolling-window settlement cap (survives restarts —
            # it's re-derived from the ledger, not an in-process counter). Persisted so a
            # rehydrated mandate keeps the exact same limit.
            n = cfg.get("rate_per_min")
            return SpendPolicy(rolling_window_seconds=60,
                               rolling_window_settlement_cap=int(n)) if n else None

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
                spend_policy=_policy(cfg),
            )
            self.engine.rehydrate_mandate(mandate)
            self.merchant_account_id = cfg["merchant_account_id"]
            log.info("rehydrated mandate %s (budget %s cents, rate/min %s) from %s",
                     mandate.mandate_id, cfg["budget_cap_cents"], cfg.get("rate_per_min"), cfg_path)
            if rate_per_min is not None and rate_per_min != cfg.get("rate_per_min"):
                log.warning("MANDATEHUB_RATE_PER_MIN=%s ignored — mandate.json has %s "
                            "(edit mandate.json + restart to change the limit)",
                            rate_per_min, cfg.get("rate_per_min"))
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
                rate_per_min=rate_per_min,
            )
            self.engine.create_mandate(
                mandate_id=cfg["mandate_id"], principal_id=cfg["principal_id"],
                escrow_account_id=escrow.account_id, budget_cap=Money(budget_cents, USDC),
                allowed_purposes=frozenset(["API_CALL"]),
                valid_from=boot - timedelta(minutes=1), valid_until=boot + timedelta(days=365),
                created_at=boot, spend_policy=_policy(cfg),
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

    def handle_payment(self, x_payment: str | None, requirements=None) -> tuple[int, dict, dict[str, str]]:
        req = requirements or self.requirements
        if not x_payment:
            return 402, {"x402Version": 1, "error": "payment required",
                         "accepts": [req.to_wire()]}, {}
        try:
            payload = decode_x_payment(x_payment)
        except Exception:
            self.denied_count += 1
            return 402, {"x402Version": 1, "error": "malformed X-PAYMENT",
                         "accepts": [req.to_wire()]}, {}
        auth = payload.payload.authorization
        if not (isinstance(auth.value, str) and auth.value.isdigit() and int(auth.value) > 0):
            self.denied_count += 1
            return 402, {"x402Version": 1, "error": "malformed X-PAYMENT",
                         "detail": "authorization.value must be a positive base-10 integer string",
                         "accepts": [req.to_wire()]}, {}
        at = _now()
        ok, reason, _ = self.engine.preauthorize(
            mandate_id=self.mandate_id, intent_id=auth.nonce,
            payee_account_id=self.merchant_account_id,
            amount=Money(int(auth.value), USDC), purpose="API_CALL", at=at)
        if not ok:
            self.denied_count += 1
            log.info("DENY %s (mandate gate; facilitator not called)", reason)
            code = 429 if reason in ("WINDOW_VELOCITY_EXCEEDED", "EPOCH_VELOCITY_EXCEEDED") else 402
            return code, {"x402Version": 1, "error": "rejected by mandate",
                          "mandateReason": reason,
                          "accepts": [req.to_wire()]}, {}
        v = self.adapter.verify(payload, req)
        if not v.is_valid:
            self.denied_count += 1
            log.info("DENY facilitator verify: %s", v.invalid_reason)
            return 402, {"x402Version": 1, "error": "payment invalid",
                         "invalidReason": v.invalid_reason,
                         "accepts": [req.to_wire()]}, {}
        s = self.adapter.settle(payload, req)
        if not s.success:
            self.denied_count += 1
            log.warning("DENY facilitator settle: %s", s.error_reason)
            return 402, {"x402Version": 1, "error": "settlement failed",
                         "errorReason": s.error_reason,
                         "accepts": [req.to_wire()]}, {}
        r = self.engine.settle_intent(
            mandate_id=self.mandate_id, intent_id=auth.nonce,
            payee_account_id=self.merchant_account_id,
            amount=Money(int(auth.value), USDC), purpose="API_CALL", at=at)
        if r.decision != "SETTLED":
            # On-chain settlement already happened but the ledger refused to book it
            # (e.g. a concurrent claim/budget race on a shared store). Surface loudly for
            # manual reconciliation — never pretend success, never drop the tx hash.
            log.critical("LEDGER/CHAIN DIVERGENCE: on-chain tx %s settled but ledger denied "
                         "%s (%s) — manual reconciliation required", s.transaction,
                         auth.nonce[:18], r.reason)
            return 500, {"x402Version": 1, "error": "settled on-chain but not booked",
                         "reason": r.reason,
                         "settlement": {"transaction": s.transaction, "network": s.network}}, {}
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


def _v2_challenge(requirements, public_url: str) -> dict:
    """An x402 v2 `402` body for Bazaar discovery (CDP validate). CAIP-2 network, `amount`,
    and the `extensions.bazaar` block. Iterated against POST …/x402/validate until green."""
    r = requirements
    caip2 = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}.get(r.network, r.network)
    return {
        "x402Version": 2,
        "error": "payment required",
        "resource": {"url": f"{public_url}/quote-v2"},
        "accepts": [{
            "scheme": r.scheme,
            "network": caip2,
            "amount": r.max_amount_required,
            "asset": r.asset,
            "payTo": r.pay_to,
            "resource": f"{public_url}/quote-v2",
            "description": "mandatehub: a mandate-gated price quote. Pays real USDC on Base; "
                           "every 200 carries an on-chain settlement tx + a ProofOfMandate.",
            "mimeType": "application/json",
            "maxTimeoutSeconds": r.max_timeout_seconds,
            "extra": dict(r.extra or {}),
        }],
        "extensions": {"bazaar": _bazaar_extension(public_url)},
    }


def _bazaar_extension(public_url: str) -> dict:
    """x402 Bazaar (CDP discovery) extension for /quote-v2. `schema` is a JSON Schema over the
    `info` object (input + output) — matching the shape of a listed resource."""
    output_example = {
        "data": {"quote": "BTC/USD 68,000", "ts": "2026-07-21T00:00:00+00:00"},
        "settlement": {"transaction": "0x...", "network": "base"},
        "proofOfMandate": {"remaining_cents": 9990000, "total_settled_cents": 10000,
                           "is_within_budget": True, "is_collateralized": True,
                           "audit_log_root_hash": "..."},
    }
    return {
        "routeTemplate": "/quote-v2",
        "info": {
            "input": {"type": "http", "method": "GET"},
            "output": {"type": "json", "example": output_example},
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["input", "output"],
            "properties": {
                "input": {
                    "type": "object", "additionalProperties": False,
                    "required": ["type", "method"],
                    "properties": {"type": {"type": "string"},
                                   "method": {"type": "string", "enum": ["GET"]}},
                },
                "output": {
                    "type": "object", "additionalProperties": False,
                    "required": ["type", "example"],
                    "properties": {
                        "type": {"type": "string"},
                        "example": {
                            "type": "object",
                            "required": ["data", "settlement", "proofOfMandate"],
                            "properties": {
                                "data": {"type": "object"},
                                "settlement": {"type": "object", "properties": {
                                    "transaction": {"type": "string"},
                                    "network": {"type": "string"}}},
                                "proofOfMandate": {"type": "object", "properties": {
                                    "remaining_cents": {"type": "integer"},
                                    "is_within_budget": {"type": "boolean"},
                                    "is_collateralized": {"type": "boolean"}}},
                            },
                        },
                    },
                },
            },
        },
    }


def _esc(x: object) -> str:
    import html
    return html.escape(str(x))


def _dashboard_html(op: "Operator", public_url: str) -> str:
    """Server-rendered, no-JS live dashboard (works under a strict CSP)."""
    from _metrics import compute_metrics
    at = _now()
    h = op.health()
    m = compute_metrics(op.ledger, now=at)
    net = op.requirements.network
    price = int(op.requirements.max_amount_required) / 1e6
    rows = "".join(
        f"<tr><td>{_esc(day)}</td><td style='text-align:right'>{d['count']}</td>"
        f"<td style='text-align:right'>{d['revenue_cents']/1e6:.6f}</td></tr>"
        for day, d in m["per_day"].items()
    ) or "<tr><td colspan='3' style='color:#888'>no settlements yet</td></tr>"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>mandatehub operator — live</title><style>
:root{{color-scheme:light dark}}
body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
max-width:760px;margin:0 auto;padding:48px 20px}}
h1{{margin:0 0 .1em}}.tag{{color:#888;margin:0 0 1.6em}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:24px 0}}
.card{{border:1px solid #8884;border-radius:12px;padding:16px}}
.card .n{{font-size:1.7rem;font-weight:600}}.card .l{{color:#888;font-size:.85rem}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
td,th{{padding:6px 8px;border-bottom:1px solid #8883;text-align:left}}
code,pre{{font-family:ui-monospace,Menlo,monospace}}
pre{{background:#8881;border-radius:10px;padding:12px;overflow:auto}}
a{{color:#1a56db}}@media(prefers-color-scheme:dark){{a{{color:#6ea8ff}}}}
.dot{{color:#2ea043}}</style></head><body>
<h1><span class=dot>●</span> mandatehub operator</h1>
<p class=tag>A live x402 resource server with a mandate gate — budget-capped, replay-proof,
proof-carrying payments settling real USDC on <b>{_esc(net)}</b>.</p>
<div class=grid>
<div class=card><div class=n>{m['settlements']}</div><div class=l>settlements</div></div>
<div class=card><div class=n>{m['revenue_cents']/1e6:.4f}</div><div class=l>USDC revenue</div></div>
<div class=card><div class=n>{m['unique_payees']}</div><div class=l>unique payees</div></div>
<div class=card><div class=n>{h['remaining_cents']/1e6:.2f}</div><div class=l>USDC budget left</div></div>
</div>
<h3>Pay it</h3>
<pre>pip install 'mandatehub[evm]'
export MANDATEHUB_AGENT_PRIVATE_KEY=0x...   # a {_esc(net)}-funded key
python examples/x402_pay.py {_esc(public_url)}/quote</pre>
<p>Price: <b>{price:.6f} USDC</b> per call · endpoints:
<a href="{_esc(public_url)}/healthz">/healthz</a> ·
<a href="{_esc(public_url)}/metrics">/metrics</a> · <code>/quote</code> (402 → pay).</p>
<h3>Settlements by day</h3>
<table><tr><th>day</th><th style='text-align:right'>calls</th>
<th style='text-align:right'>USDC</th></tr>{rows}</table>
<p class=tag style="margin-top:2em">audit root <code>{_esc(h['audit_root'][:16])}…</code> ·
<a href="https://github.com/Hiroshi-Ichiyanagi/mandatehub">source</a> ·
<a href="https://mandatehub.ichiyanagi1111.workers.dev">about</a><br>
Self-funded pilot; mainnet is the operator's call, not an audited guarantee.</p>
</body></html>"""


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = _require("MANDATEHUB_FACILITATOR_URL")
    pay_to = _require("MANDATEHUB_PAY_TO")
    port = int(os.environ.get("MANDATEHUB_PORT", "8403"))
    data_dir = Path(os.environ.get("MANDATEHUB_DATA_DIR",
                                   str(Path.home() / ".mandatehub-operator")))
    price = os.environ.get("MANDATEHUB_AMOUNT", "10000")
    budget = int(os.environ.get("MANDATEHUB_BUDGET", "1000000"))
    network = os.environ.get("MANDATEHUB_NETWORK", "base-sepolia")
    rate_per_min = int(os.environ["MANDATEHUB_RATE_PER_MIN"]) if os.environ.get("MANDATEHUB_RATE_PER_MIN") else None
    db_url = os.environ.get("MANDATEHUB_DB_URL")  # e.g. "dbname=mandatehub host=/tmp" → shared Postgres ledger

    # Network-aware defaults. On mainnet the EIP-712 domain name is "USD Coin"
    # (on-chain-verified) — reusing the Sepolia extra would invalidate every signature.
    if network == "base":
        default_asset, extra = BASE_MAINNET_USDC, dict(BASE_MAINNET_USDC_DOMAIN)
    else:
        default_asset, extra = BASE_SEPOLIA_USDC, {"name": "USDC", "version": "2"}
    public_url = os.environ.get("MANDATEHUB_PUBLIC_URL", f"http://127.0.0.1:{port}")
    requirements = X402PaymentRequirements(
        scheme="exact", network=network, max_amount_required=price,
        asset=os.environ.get("MANDATEHUB_ASSET", default_asset), pay_to=pay_to,
        resource=f"{public_url}/quote", max_timeout_seconds=60,
        description="one API call (mandatehub operator)",
        mime_type="application/json",   # CDP validates v1 requirements strictly
        extra=extra,
    )
    # Facilitator auth: if a CDP key file is configured (or the URL is CDP), attach the
    # per-request JWT hook. The key secret never leaves the hook.
    header_hook = None
    cdp_key_file = os.environ.get("MANDATEHUB_CDP_KEY_FILE")
    if cdp_key_file or "api.cdp.coinbase.com" in url:
        from mandatehub.signers import cdp_header_hook_from_file
        header_hook = cdp_header_hook_from_file(cdp_key_file or "~/.mandatehub-cdp.json",
                                                facilitator_url=url)
    op = Operator(data_dir, adapter=RemoteFacilitatorAdapter(url, network=network,
                                                             header_hook=header_hook),
                  requirements=requirements, budget_cents=budget,
                  rate_per_min=rate_per_min, db_url=db_url)
    import dataclasses as _dc
    requirements_v2 = _dc.replace(requirements, resource=f"{public_url}/quote-v2")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                status, body, extra = 200, op.health(), {}
            elif self.path == "/quote-v2":
                xp = self.headers.get("X-PAYMENT")
                if xp:  # a payment arrived -> settle via CDP (same money-path as /quote)
                    status, body, extra = op.handle_payment(xp, requirements_v2)
                else:
                    import base64 as _b64
                    body = _v2_challenge(op.requirements, public_url)
                    # x402 v2: the indexer reads the PaymentRequired payload ONLY from this header.
                    hdr = _b64.b64encode(json.dumps(body).encode()).decode()
                    status, extra = 402, {"PAYMENT-REQUIRED": hdr}
            elif self.path == "/metrics":
                from _metrics import compute_metrics
                status, extra = 200, {}
                body = compute_metrics(op.ledger, now=_now())
            elif self.path == "/" or self.path == "":
                # Browsers get a human dashboard; API clients get JSON.
                if "text/html" in (self.headers.get("Accept") or ""):
                    html = _dashboard_html(op, public_url).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'none'; style-src 'unsafe-inline'; "
                                     "img-src 'self' https:; base-uri 'none'")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return
                status, extra = 200, {}
                body = {
                    "service": "mandatehub operator",
                    "what": "x402 resource server with a mandate gate: budget-capped, "
                            "replay-proof, proof-carrying autonomous payments",
                    "network": op.requirements.network,
                    "price_minor_units": op.requirements.max_amount_required,
                    "pay": f"GET {public_url}/quote (returns 402 + accepts; pay via the "
                           "x402 exact scheme, e.g. pip install mandatehub)",
                    "health": f"{public_url}/healthz",
                    "metrics": f"{public_url}/metrics",
                    "library": "https://github.com/Hiroshi-Ichiyanagi/mandatehub",
                    "site": "https://mandatehub.ichiyanagi1111.workers.dev",
                    "bazaar": _bazaar_extension(public_url),
                }
            elif self.path == "/quote":
                status, body, extra = op.handle_payment(self.headers.get("X-PAYMENT"))
            else:
                status, body, extra = 404, {"error": "not found",
                                            "endpoints": ["/", "/healthz", "/metrics",
                                                          "/quote", "/quote-v2"]}, {}
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
