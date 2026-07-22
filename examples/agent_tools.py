#!/usr/bin/env python3
"""mandatehub as tools for popular agent frameworks (LangChain, CrewAI).

A small, dependency-light `MandatehubClient` (discover / preview / purchase over the live
x402 service) plus thin adapters that expose it as native tools in LangChain and CrewAI.
The frameworks are OPTIONAL — import guards mean this file loads (and its client is usable)
without either installed.

    pip install 'mandatehub[evm]'                 # for purchase(); preview/discover need only stdlib
    pip install langchain-core                    # for langchain_tools()
    pip install crewai                            # for crewai_tools()

    export MANDATEHUB_AGENT_PRIVATE_KEY=0x...      # a funded Base wallet; omit to preview only
    export MANDATEHUB_BASE_URL=https://mandatehub.obolpay.xyz   # optional; this is the default

Wire into an agent (LangChain):
    from examples.agent_tools import langchain_tools
    tools = langchain_tools()                      # discover / preview / purchase
    agent = create_react_agent(llm, tools)

Wire into a crew (CrewAI):
    from examples.agent_tools import crewai_tools
    agent = Agent(role="buyer", tools=crewai_tools(), ...)
"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.error import HTTPError

UA = {"User-Agent": "mandatehub-agent-tools/1.0"}


class MandatehubClient:
    """Consume the live mandatehub x402 service. discover/preview are free; purchase spends USDC."""

    def __init__(self, base_url: str | None = None, private_key: str | None = None):
        self.base = (base_url or os.environ.get(
            "MANDATEHUB_BASE_URL", "https://mandatehub.obolpay.xyz")).rstrip("/")
        self.private_key = private_key or os.environ.get("MANDATEHUB_AGENT_PRIVATE_KEY")

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        url = self.base + path if path.startswith("/") else path
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

    def discover(self) -> dict:
        """List every payable product, its path, params, price, and how-to-pay (free)."""
        return self._get("/.well-known/agents.json")[1]

    def preview(self, path: str = "/quote") -> dict:
        """Read a product's 402 payment terms without signing or spending anything (free)."""
        status, body = self._get(path)
        if status == 503:
            return {"status": 503, "note": "temporarily unavailable — you would not be charged"}
        if status != 402:
            return {"status": status, "note": "endpoint did not challenge with 402", "body": body}
        a = (body.get("accepts") or [{}])[0]
        return {"path": path, "price_minor_units": a.get("maxAmountRequired") or a.get("amount"),
                "network": a.get("network"), "asset": a.get("asset"),
                "pay_to": a.get("payTo") or a.get("pay_to"),
                "price_note": "minor units of USDC (6 decimals): 10000 = 0.01 USDC"}

    def purchase(self, path: str = "/quote") -> dict:
        """Pay the quoted USDC and return {data, settlement, proofOfMandate}. Spends real USDC;
        requires a funded Base key. Uses the library's own exact x402 payload builder."""
        if not self.private_key:
            return {"error": "no private key (set MANDATEHUB_AGENT_PRIVATE_KEY); use preview() instead"}
        status, body = self._get(path)
        if status == 503:
            return {"error": "temporarily unavailable — not charged"}
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
            signer = EthAccountSigner(self.private_key)
        except Exception as e:  # noqa: BLE001
            return {"error": f"signer setup failed (pip install 'mandatehub[evm]'): {e}"}
        reqs = X402PaymentRequirements.from_wire(accepts[0])
        header = encode_x_payment(ExactEvmPayloadBuilder(signer, network=reqs.network).build(reqs))
        status, body = self._get(path, {"X-PAYMENT": header})
        if status != 200:
            return {"error": f"payment not accepted ({status})",
                    "reason": body.get("mandateReason") or body.get("invalidReason")
                    or body.get("errorReason")}
        return {"paid_as": signer.address, "data": body.get("data"),
                "settlement": body.get("settlement"), "proofOfMandate": body.get("proofOfMandate")}


# --------------------------------------------------------------------------- LangChain

def langchain_tools(client: MandatehubClient | None = None) -> list:
    """Return [discover, preview, purchase] as LangChain StructuredTools.
    Requires `pip install langchain-core`."""
    from langchain_core.tools import StructuredTool  # optional dep

    c = client or MandatehubClient()
    return [
        StructuredTool.from_function(
            name="mandatehub_discover", func=lambda: c.discover(),
            description="List mandatehub's payable products, paths, params, and prices. Free."),
        StructuredTool.from_function(
            name="mandatehub_preview", func=lambda path="/quote": c.preview(path),
            description="Read a product's x402 payment terms (price/network) without paying. "
                        "path is a product path from discover(), e.g. '/quote'. Free."),
        StructuredTool.from_function(
            name="mandatehub_purchase", func=lambda path="/quote": c.purchase(path),
            description="Pay the quoted USDC for a product and return its data, on-chain "
                        "settlement tx, and ProofOfMandate. Spends real USDC on Base."),
    ]


# ------------------------------------------------------------------------------- CrewAI

def crewai_tools(client: MandatehubClient | None = None) -> list:
    """Return [discover, preview, purchase] as CrewAI tools. Requires `pip install crewai`."""
    from crewai.tools import tool  # optional dep

    c = client or MandatehubClient()

    @tool("mandatehub_discover")
    def discover() -> dict:
        """List mandatehub's payable products, paths, params, and prices. Free."""
        return c.discover()

    @tool("mandatehub_preview")
    def preview(path: str = "/quote") -> dict:
        """Read a product's x402 payment terms without paying. Free."""
        return c.preview(path)

    @tool("mandatehub_purchase")
    def purchase(path: str = "/quote") -> dict:
        """Pay the quoted USDC and return data + settlement tx + ProofOfMandate. Spends USDC."""
        return c.purchase(path)

    return [discover, preview, purchase]


if __name__ == "__main__":
    # A free, no-key demo: discover the catalog and preview a couple of products.
    c = MandatehubClient()
    cat = c.discover()
    print(f"service: {cat.get('service')} — {len(cat.get('products', []))} products")
    for p in cat.get("products", [])[:3]:
        print(f"  {p['id']:<14} {p['path']}")
    print("preview /quote:", json.dumps(c.preview("/quote")))
