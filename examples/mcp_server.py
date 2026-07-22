#!/usr/bin/env python3
"""mandatehub MCP server — expose the live x402 service as native tools for AI agents.

Any MCP-compatible agent (Claude Desktop, Cursor, etc.) can add this server and then
autonomously DISCOVER -> PREVIEW -> PURCHASE verifiable data & verification results, each
call settling ~0.01 USDC on Base and returning canonically-hashed JSON + an on-chain
settlement tx + a ProofOfMandate.

It reuses the library's own client path (the exact x402 EIP-3009/EIP-712 payload builder),
so it can never drift from what `examples/x402_pay.py` — or a real agent — actually sends.

Install & run:
    pip install "mcp[cli]" "mandatehub[evm]"
    # purchase() needs a funded Base wallet (>= the quoted USDC + it is gasless on the client):
    export MANDATEHUB_AGENT_PRIVATE_KEY=0x...
    export MANDATEHUB_BASE_URL=https://mandatehub.obolpay.xyz   # optional; this is the default
    python examples/mcp_server.py

Claude Desktop config (claude_desktop_config.json):
    {
      "mcpServers": {
        "mandatehub": {
          "command": "python",
          "args": ["/absolute/path/to/examples/mcp_server.py"],
          "env": { "MANDATEHUB_AGENT_PRIVATE_KEY": "0x..." }
        }
      }
    }

Tools:
    discover()               -> the machine-readable agent catalog (free)
    openapi()                -> the OpenAPI 3.1 spec (free)
    preview(path)            -> the 402 payment terms for a product, sign/spend nothing (free)
    purchase(path)           -> pay the quoted USDC and return {data, settlement, proof} (spends real USDC)
"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.error import HTTPError

from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("MANDATEHUB_BASE_URL", "https://mandatehub.obolpay.xyz").rstrip("/")
# Public edges (Cloudflare) 403 the literal default urllib UA; always send a real one.
UA = {"User-Agent": "mandatehub-mcp/1.0"}

mcp = FastMCP("mandatehub")


def _get(path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    url = BASE + path if path.startswith("/") else path
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"_raw": e.read()[:200].decode("utf-8", "replace")}


@mcp.tool()
def discover() -> dict:
    """Return mandatehub's machine-readable agent catalog: every payable product, its path,
    params, price, and how-to-pay. No payment required — start here to choose a product."""
    _, body = _get("/.well-known/agents.json")
    return body


@mcp.tool()
def openapi() -> dict:
    """Return the OpenAPI 3.1 spec for the service (per-path x-402-payment terms included).
    No payment required."""
    _, body = _get("/openapi.json")
    return body


@mcp.tool()
def preview(path: str = "/quote") -> dict:
    """Fetch a product's HTTP 402 challenge and return the payment TERMS (network, asset,
    amount, recipient) without signing or spending anything. `path` is a product path from
    discover(), e.g. "/quote", "/product/fx?from=USD&to=JPY&amount=1000000",
    "/product/verify-tx?tx=0x…". No payment required."""
    status, body = _get(path)
    if status == 503:
        return {"status": 503, "note": "product temporarily unavailable — you would not be charged",
                "body": body}
    if status != 402:
        return {"status": status, "note": "endpoint did not challenge with 402", "body": body}
    accepts = body.get("accepts") or []
    if not accepts:
        return {"status": 402, "note": "402 without 'accepts'", "body": body}
    a = accepts[0]
    amt = a.get("maxAmountRequired") or a.get("amount")
    return {
        "path": path,
        "price_minor_units": amt,
        "price_note": "minor units of USDC (6 decimals): 10000 = 0.01 USDC",
        "network": a.get("network"),
        "asset": a.get("asset"),
        "pay_to": a.get("payTo") or a.get("pay_to"),
        "how_to_pay": "call purchase(path) with the same path; requires MANDATEHUB_AGENT_PRIVATE_KEY",
    }


@mcp.tool()
def purchase(path: str = "/quote") -> dict:
    """Pay the quoted USDC for a product and return {data, settlement, proofOfMandate}.
    `path` is a product path from discover(). Requires env MANDATEHUB_AGENT_PRIVATE_KEY (a
    Base wallet holding >= the quoted USDC). WARNING: this settles real USDC on-chain.

    The payment is built with the library's own exact x402 (EIP-3009/EIP-712) builder, so the
    server's mandate gate (budget, replay, rate) applies exactly as for any other client."""
    key = os.environ.get("MANDATEHUB_AGENT_PRIVATE_KEY")
    if not key:
        return {"error": "MANDATEHUB_AGENT_PRIVATE_KEY not set; cannot pay. Use preview(path) instead."}

    status, body = _get(path)
    if status == 503:
        return {"error": "product temporarily unavailable — not charged", "body": body}
    if status != 402:
        return {"error": f"endpoint did not challenge with 402 (got {status})", "body": body}
    accepts = body.get("accepts") or []
    if not accepts:
        return {"error": "402 without 'accepts'", "body": body}

    from mandatehub.x402 import (
        ExactEvmPayloadBuilder,
        X402PaymentRequirements,
        encode_x_payment,
    )
    try:
        from mandatehub.signers import EthAccountSigner
        signer = EthAccountSigner(key)
    except Exception as e:  # noqa: BLE001
        return {"error": f"signer setup failed (pip install 'mandatehub[evm]'): {e}"}

    reqs = X402PaymentRequirements.from_wire(accepts[0])
    header = encode_x_payment(ExactEvmPayloadBuilder(signer, network=reqs.network).build(reqs))
    status, body = _get(path, {"X-PAYMENT": header})
    if status != 200:
        return {"error": f"payment not accepted ({status})",
                "reason": body.get("mandateReason") or body.get("invalidReason")
                or body.get("errorReason"), "body": body}
    return {
        "paid_as": signer.address,
        "data": body.get("data"),
        "settlement": body.get("settlement"),
        "chainVerification": body.get("chainVerification"),
        "proofOfMandate": body.get("proofOfMandate"),
    }


if __name__ == "__main__":
    mcp.run()
