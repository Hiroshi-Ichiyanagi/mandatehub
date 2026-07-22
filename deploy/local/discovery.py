"""Machine-readable self-description for AI agents (stdlib only).

Serves, from the operator's own catalog, the interface definitions agents look for:
  - GET /.well-known/ai-plugin.json  — the plugin manifest (name, auth, api pointer)
  - GET /.well-known/agents.json     — a compact agent catalog (paths, params, price, how-to-pay)
  - GET /openapi.json                — an OpenAPI 3.1 spec generated from the product catalog

All generated from `products.CATALOG` + the operator's requirements, so the machine interface
can never drift from what actually sells.
"""
from __future__ import annotations

from typing import Any


def _param_schema(needs: str) -> list[dict]:
    """Turn a product's `needs` hint (e.g. "?from=USD&to=JPY&amount=<minor units>") into
    OpenAPI query-parameter objects. Best-effort; unknown shapes become a free `q`/`data`."""
    if not needs or "?" not in needs:
        return []
    q = needs.split("?", 1)[1]
    params = []
    for pair in q.split("&"):
        name = pair.split("=", 1)[0].strip()
        if not name:
            continue
        params.append({"name": name, "in": "query", "required": False,
                       "schema": {"type": "string"},
                       "description": pair})
    return params


def agents_catalog(public_url: str, price_minor: str, network: str,
                   catalog: dict) -> dict[str, Any]:
    products = [{
        "id": name,
        "path": f"/product/{name}",
        "url": f"{public_url}/product/{name}",
        "description": p.description,
        "params": p.needs or None,
        "available": p.available(),
    } for name, p in catalog.items()]
    # /quote is the default ECB product, not in /product/*
    products.insert(0, {"id": "quote", "path": "/quote", "url": f"{public_url}/quote",
                        "description": "ECB official FX reference rates (EUR base, ~30 "
                                       "currencies), canonically hashed.",
                        "params": None, "available": True})
    return {
        "schema_version": "1.0",
        "service": "mandatehub",
        "summary": "Machine-payable data & verification over x402 (HTTP 402, real USDC on Base). "
                   "Each call: pay 0.01 USDC, get canonically-hashed JSON + an on-chain "
                   "settlement tx + a ProofOfMandate.",
        "payment": {
            "protocol": "x402",
            "scheme": "exact",
            "network": network,
            "price_minor_units": price_minor,
            "price_note": "minor units of USDC (6 decimals): 10000 = 0.01 USDC",
            "how_to_pay": "GET the product URL -> 402 with `accepts`; sign an x402 `exact` "
                          "payment and resend it in the X-PAYMENT header. "
                          "Client: `pip install 'mandatehub[evm]'`, see examples/x402_pay.py.",
            "fail_closed": "Unavailable/stale products return 503 BEFORE any charge.",
        },
        "products": products,
        "openapi": f"{public_url}/openapi.json",
        "docs": "https://github.com/Hiroshi-Ichiyanagi/mandatehub/blob/main/docs/OVERVIEW.md",
    }


def ai_plugin(public_url: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "name_for_model": "mandatehub",
        "name_for_human": "mandatehub — machine-payable data & verification",
        "description_for_model":
            "Buy verifiable data and verification results for 0.01 USDC per call over x402 "
            "(HTTP 402 on Base). Products: FX reference rates, zero-spread FX conversion, LLM "
            "backend-selection matrices, audit-log verification, on-chain USDC tx verification, "
            "govern evidence-bundle verification, openunit valuation, JP-equity convergence "
            "scores. Each response is canonically hashed and carries an on-chain settlement tx "
            "plus a ProofOfMandate. To use a product: GET its URL, receive 402 with `accepts`, "
            "sign an x402 `exact` payment, resend in the X-PAYMENT header. See "
            f"{public_url}/.well-known/agents.json for the catalog.",
        "description_for_human":
            "AI agents pay ~$0.01 in USDC per API call for verifiable data & proofs.",
        "auth": {"type": "none",
                 "note": "no API key; payment is per-request via the x402 exact scheme"},
        "api": {"type": "openapi", "url": f"{public_url}/openapi.json"},
        "logo_url": "https://mandatehub.ichiyanagi1111.workers.dev/logo.svg",
        "contact_email": "",
        "legal_info_url":
            "https://github.com/Hiroshi-Ichiyanagi/mandatehub/blob/main/ROADMAP.md",
    }


def openapi_spec(public_url: str, price_minor: str, network: str,
                 catalog: dict) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    x402 = {"x-402-payment": {"protocol": "x402", "scheme": "exact", "network": network,
                              "price_minor_units": price_minor}}

    def op(name: str, path: str, desc: str, params: list[dict]) -> dict:
        return {"get": {
            "operationId": f"buy_{name.replace('-', '_')}",
            "summary": desc,
            "parameters": params,
            **x402,
            "responses": {
                "200": {"description": "paid: product JSON + settlement tx + ProofOfMandate",
                        "content": {"application/json": {"schema": {"type": "object"}}}},
                "402": {"description": "payment required (x402 `accepts` in body/header)"},
                "503": {"description": "product temporarily unavailable — not charged"},
            }}}

    paths["/quote"] = op("quote", "/quote", "ECB official FX reference rates", [])
    for name, p in catalog.items():
        paths[f"/product/{name}"] = op(name, f"/product/{name}", p.description,
                                       _param_schema(p.needs))
    return {
        "openapi": "3.1.0",
        "info": {"title": "mandatehub — machine-payable products", "version": "0.1.0",
                 "description": "Pay-per-call data & verification over x402 (real USDC on Base). "
                                "Every product returns canonically-hashed JSON with an on-chain "
                                "settlement tx and a ProofOfMandate.",
                 "license": {"name": "Apache-2.0"}},
        "servers": [{"url": public_url}],
        "paths": paths,
    }
