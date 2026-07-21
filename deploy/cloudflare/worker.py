"""mandatehub testnet reference resource-server — Cloudflare Python Worker (SKELETON).

Reuses `mandatehub.x402.serve_once` (a socket-free pure function) unchanged: a Worker request
maps to `serve_once(...) -> (status, body, headers)`, which maps back to a `Response`. This is
the TESTNET DEMO tier described in docs/DEPLOY_CLOUDFLARE.md — an in-memory ledger per isolate,
no mainnet value. A durable ledger (Cloudflare D1 via the LedgerStorage protocol) is gate H2.

Status: this follows the verified flow of examples/x402_facilitator.py, but it has NOT been
executed on the Workers runtime in-repo — treat the first `wrangler dev` as the confirmation.

Entry point: `on_fetch(request, env)` (Cloudflare Python Workers).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from workers import Response  # provided by the Cloudflare Python Workers runtime

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
from mandatehub.x402 import Facilitator, PaymentRequirements, serve_once

# Build the demo facilitator once per isolate. The window is anchored to "now" so requests at
# runtime fall inside it (the offline examples use a fixed T; a live edge Worker cannot).
#
# Everything that draws entropy (uuid4 in account/tx ids) or reads the clock is deferred
# into on_fetch: the Workers runtime disallows crypto.getRandomValues / Date.now in global
# scope (module import happens at snapshot/deploy time), so a module-level build would fail
# to load. Lazy per-isolate init keeps it inside a request context.
_FAC = None
_PAYEE = None


def _usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _build_facilitator(boot: datetime) -> "tuple[Facilitator, str]":
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=boot)
    b.transfer(plat.account_id, escrow.account_id, _usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=boot)
    payee = ledger.open_account(OwnerType.USER, Currency.USDC, "api-provider")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="m1", principal_id="agent", escrow_account_id=escrow.account_id,
        budget_cap=_usdc(100), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=boot - timedelta(days=1), valid_until=boot + timedelta(days=365),
        created_at=boot, per_transaction_limit=_usdc(40),
    )
    return Facilitator(eng), payee.account_id


def _requirements(resource: str, max_amount_cents: int) -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact", network=_FAC.network, max_amount_required_cents=max_amount_cents,
        resource=resource, description="one API call (testnet demo)", pay_to=_PAYEE,
        asset="USDC", mandate_id="m1", purpose="API_CALL",
    )


async def on_fetch(request, env):  # noqa: ANN001 — Workers runtime signature
    global _FAC, _PAYEE
    if _FAC is None:
        _FAC, _PAYEE = _build_facilitator(datetime.now(timezone.utc))

    max_amount_cents = int(getattr(env, "MANDATEHUB_MAX_AMOUNT", "10000") or "10000")
    resource = getattr(env, "MANDATEHUB_RESOURCE", request.url)

    # serve_once only reads the payment header; fetch it via .get() (guaranteed across
    # Workers SDK versions) instead of relying on Headers iteration semantics.
    sig = request.headers.get("PAYMENT-SIGNATURE")
    headers = {"PAYMENT-SIGNATURE": sig} if sig else {}

    reqs = _requirements(resource, max_amount_cents)
    at = datetime.now(timezone.utc)
    status, body, resp_headers = serve_once(
        _FAC, reqs, headers,
        lambda: {"quote": "BTC/USD 68,000", "ts": at.isoformat()},
        at=at,
    )

    out = dict(resp_headers)
    out["Content-Type"] = "application/json"
    return Response(json.dumps(body), status=status, headers=out)
