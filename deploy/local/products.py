"""Real sellable products for the mandatehub operator (stdlib only).

1. **ECB FX reference rates** (`ecb_quote`) — the European Central Bank's official daily
   euro reference rates (free public XML), parsed, canonically hashed (sorted-key JSON →
   sha256) and cached. The artifact hash makes every paid response byte-reproducible and
   third-party checkable. A freshness gate (`ecb_available`) lets the operator refuse to
   charge when data can't be served (deny BEFORE settlement — the fail-closed SLA pattern
   from the x402-gateway playbook).

2. **On-chain settlement verification** (`verify_usdc_tx`) — independently confirm a Base
   USDC transfer: fetch the tx receipt via JSON-RPC, check status, and decode the ERC-20
   Transfer log (from/to/value). This is both a paid product (/verify-tx) and the operator's
   own post-settle chain confirmation (THREAT_MODEL gap #5: don't take the facilitator's
   word for it).

No third-party deps; observation never blocks the money path (callers decide how failures
map to HTTP).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from xml.etree import ElementTree

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_UA = {"User-Agent": "mandatehub-operator/1 (+https://github.com/Hiroshi-Ichiyanagi/mandatehub)"}

_cache: dict = {"at": 0.0, "data": None}
ECB_TTL_SECONDS = 900          # ECB updates once per business day; 15 min cache is generous
ECB_MAX_AGE_SECONDS = 86400 * 4  # long weekend tolerance; older than this = stale (503)


def _parse_ecb(xml_bytes: bytes) -> dict:
    """ECB daily reference XML -> {"date": "YYYY-MM-DD", "rates": {"USD": "1.0876", ...}}."""
    root = ElementTree.fromstring(xml_bytes)
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    day = root.find(".//e:Cube[@time]", ns)
    if day is None:
        raise ValueError("ECB XML: no dated Cube")
    rates = {c.get("currency"): c.get("rate") for c in day.findall("e:Cube", ns)}
    if not rates or "USD" not in rates:
        raise ValueError("ECB XML: no rates parsed")
    return {"date": day.get("time"), "rates": dict(sorted(rates.items()))}


def _fetch_ecb() -> dict:
    with urllib.request.urlopen(
        urllib.request.Request(ECB_URL, headers=_UA), timeout=15
    ) as r:
        return _parse_ecb(r.read())


def ecb_quote(now: float | None = None) -> dict | None:
    """The paid payload, or None if no fresh-enough data can be served (charge nothing)."""
    t = time.time() if now is None else now
    if _cache["data"] is None or t - _cache["at"] > ECB_TTL_SECONDS:
        try:
            _cache["data"] = _fetch_ecb()
            _cache["at"] = t
        except Exception:
            if _cache["data"] is None or t - _cache["at"] > ECB_MAX_AGE_SECONDS:
                return None  # nothing servable — the operator must NOT charge
    d = _cache["data"]
    body = {"product": "ecb-fx-reference", "base": "EUR",
            "ecb_date": d["date"], "rates": d["rates"]}
    body["artifact_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def ecb_available() -> bool:
    """Freshness gate: called BEFORE settlement so stale/absent data is never charged for."""
    return ecb_quote() is not None


# --- on-chain settlement verification (Base USDC) --------------------------------------

# keccak256("Transfer(address,address,uint256)") — the ERC-20 Transfer event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc(rpc_url: str, method: str, params: list) -> dict | None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body,
                                 headers={**_UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("result")


def verify_usdc_tx(tx_hash: str, *, rpc_url: str = "https://mainnet.base.org",
                   usdc: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913") -> dict:
    """Independently verify a Base USDC transfer tx. Never raises; states what it found."""
    if not (isinstance(tx_hash, str) and tx_hash.startswith("0x") and len(tx_hash) == 66):
        return {"tx": tx_hash, "verdict": "INVALID_HASH"}
    try:
        receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
    except Exception as e:
        return {"tx": tx_hash, "verdict": "RPC_UNAVAILABLE", "detail": str(e)[:80]}
    if receipt is None:
        return {"tx": tx_hash, "verdict": "NOT_FOUND_OR_PENDING"}
    ok = receipt.get("status") == "0x1"
    transfers = []
    for lg in receipt.get("logs", []):
        if (lg.get("address", "").lower() == usdc.lower()
                and lg.get("topics") and lg["topics"][0] == TRANSFER_TOPIC):
            transfers.append({
                "from": "0x" + lg["topics"][1][-40:],
                "to": "0x" + lg["topics"][2][-40:],
                "value_minor_units": int(lg["data"], 16),
            })
    return {
        "tx": tx_hash,
        "verdict": "SUCCESS" if ok and transfers else ("REVERTED" if not ok else "NO_USDC_TRANSFER"),
        "block": int(receipt["blockNumber"], 16),
        "usdc_transfers": transfers,
        "rpc": rpc_url,
    }
