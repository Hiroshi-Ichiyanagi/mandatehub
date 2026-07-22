"""Product catalog for the mandatehub operator — real, sellable, machine-payable goods.

Each product is a `Product(description, available, build)`:
  - available() -> bool     : can we serve it right now? (freshness / dependency gate). The
                              operator calls this BEFORE settlement so a customer is never
                              charged for something we can't deliver (SLA fail-closed).
  - build(params) -> dict   : the paid JSON payload (or {"error": ...} — but build is only
                              called after available() passed and payment settled).

All stdlib-only. Assets that back a product are vendored under deploy/local/assets/.
Sources of the ideas: the x402-gateway (ECB feed + SLA pattern), qswap (measured backend
matrices), genesis_finance (zero-spread FX disclosure), genesis-keystone (audit-anchor
verification), govern (offline bundle verification).
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

_ASSETS = Path(__file__).resolve().parent / "assets"
_UA = {"User-Agent": "mandatehub-operator/1 (+https://github.com/Hiroshi-Ichiyanagi/mandatehub)"}


@dataclass(frozen=True)
class Product:
    description: str
    available: Callable[[], bool]
    build: Callable[[dict], dict]
    needs: str = ""  # human note on required query params, "" if none


def _canonical_hash(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ── 1. ECB FX reference rates (live feed, cached, SLA-gated) ───────────────────────────

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ecb_cache: dict = {"at": 0.0, "data": None}
ECB_TTL_SECONDS = 900
ECB_MAX_AGE_SECONDS = 86400 * 4


def _parse_ecb(xml_bytes: bytes) -> dict:
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
    with urllib.request.urlopen(urllib.request.Request(ECB_URL, headers=_UA), timeout=15) as r:
        return _parse_ecb(r.read())


def _ecb_data(now: float | None = None) -> dict | None:
    t = time.time() if now is None else now
    if _ecb_cache["data"] is None or t - _ecb_cache["at"] > ECB_TTL_SECONDS:
        try:
            _ecb_cache["data"] = _fetch_ecb()
            _ecb_cache["at"] = t
        except Exception:
            if _ecb_cache["data"] is None or t - _ecb_cache["at"] > ECB_MAX_AGE_SECONDS:
                return None
    return _ecb_cache["data"]


def ecb_quote(now: float | None = None) -> dict | None:
    d = _ecb_data(now)
    if d is None:
        return None
    body = {"product": "ecb-fx-reference", "base": "EUR", "ecb_date": d["date"],
            "rates": d["rates"]}
    body["artifact_sha256"] = _canonical_hash(body)
    return body


def ecb_available() -> bool:
    return _ecb_data() is not None


# ── 2. Zero-spread FX conversion + disclosure (genesis_finance idea, ECB-backed) ───────

def fx_convert(params: dict) -> dict:
    d = _ecb_data()
    frm = (params.get("from") or "EUR").upper()
    to = (params.get("to") or "USD").upper()
    amount_raw = params.get("amount") or "0"
    per_eur = {"EUR": "1.0", **(d["rates"] if d else {})}
    if frm not in per_eur or to not in per_eur:
        return {"error": "unknown currency", "supported": sorted(per_eur)}
    if not str(amount_raw).isdigit() or int(amount_raw) <= 0:
        return {"error": "amount must be a positive integer in minor units"}
    amount = int(amount_raw)  # minor units of `from`
    rate_from, rate_to = float(per_eur[frm]), float(per_eur[to])
    # cross rate A->B = (per_eur[B] / per_eur[A]); integer minor-unit result, half-up
    cross = rate_to / rate_from
    target = (amount * rate_to * 1000 // int(rate_from * 1000)) if False else round(amount * cross)
    body = {
        "product": "fx-transparency", "ecb_date": d["date"] if d else None,
        "conversion": {"from": frm, "to": to, "from_minor_units": amount,
                       "to_minor_units": target,
                       "cross_rate": f"{cross:.8f}", "spread_bps": 0,
                       "explicit_fee_minor_units": 0},
        "disclosure": {"rate_source": "ECB daily reference (median-of-one)",
                       "rate_from_per_eur": per_eur[frm], "rate_to_per_eur": per_eur[to],
                       "method": "target = round(amount * per_eur[to] / per_eur[from])"},
    }
    body["artifact_sha256"] = _canonical_hash(body)
    return body


# ── 3. qswap measured backend matrices (static, vendored) ──────────────────────────────

_QSWAP = _ASSETS / "qswap"


def qswap_matrix(params: dict) -> dict:
    which = (params.get("matrix") or "both").lower()
    out: dict = {"product": "qswap-backend-matrix",
                 "measured": "Apple Silicon; llama.cpp / mlx / candle (see docs/phase2)"}
    if which in ("fidelity", "both"):
        out["fidelity"] = json.loads((_QSWAP / "fidelity-matrix.json").read_text())
    if which in ("swap", "both"):
        out["swap"] = json.loads((_QSWAP / "swap-matrix.json").read_text())
    out["artifact_sha256"] = _canonical_hash(out)
    return out


def _qswap_available() -> bool:
    return (_QSWAP / "fidelity-matrix.json").exists() and (_QSWAP / "swap-matrix.json").exists()


# ── 4. Audit-anchor verification (genesis-keystone, vendored stdlib) ───────────────────

_KEYSTONE = _ASSETS / "keystone"


def _keystone_available() -> bool:
    return (_KEYSTONE / "anchor.py").exists() and (_KEYSTONE / "audit.py").exists()


def keystone_verify(params: dict) -> dict:
    """Verify a caller-submitted hash-chained audit log against its signed anchor.

    The chain hashes each record's timestamp, so the caller submits the EXACT stored
    records (the JSONL lines), not a reconstruction. Input:
      ?data=<base64 of {"records":[{seq,ts,event,intent_id,request_id,data,prev_hash,hash},…],
                        "anchor":{"head","length","signature","key_id"},
                        "key"(optional hex)}>
    Returns {is_valid, head, length, signed, verified_with_key}.
    """
    if str(_KEYSTONE) not in sys.path:
        sys.path.insert(0, str(_KEYSTONE))
    from anchor import SignedAnchor, verify_against_signed_anchor  # vendored
    from audit import AuditLog, AuditRecord  # vendored

    raw = params.get("data")
    if not raw:
        return {"error": "pass ?data=<base64 json {records, anchor, key?}> — records are the "
                         "exact stored AuditRecord objects (seq,ts,event,intent_id,request_id,"
                         "data,prev_hash,hash)"}
    try:
        payload = json.loads(base64.b64decode(raw))
        log = AuditLog()
        recs = [AuditRecord(**r) for r in payload["records"]]
        # load the exact records (bypass append, which would re-stamp/re-hash)
        log._records = recs
        if recs:
            log._last_hash = recs[-1].hash
            log._seq = recs[-1].seq
        a = payload["anchor"]
        anchor = SignedAnchor(head=a["head"], length=a["length"],
                              signature=a.get("signature"), key_id=a.get("key_id"))
        key = bytes.fromhex(payload["key"]) if payload.get("key") else None
        ok = verify_against_signed_anchor(log, anchor, key)
    except Exception as e:
        return {"product": "keystone-audit-verify", "is_valid": False,
                "error": f"malformed input: {type(e).__name__}"}
    return {"product": "keystone-audit-verify", "is_valid": bool(ok),
            "head": a["head"], "length": a["length"],
            "signed": a.get("signature") is not None,
            "verified_with_key": key is not None}


# ── 5. govern offline bundle verification (Rust binary, availability-gated) ─────────────

def _govern_binary() -> str | None:
    import os
    import shutil
    env = os.environ.get("MANDATEHUB_GOVERN_VERIFY")
    if env and Path(env).exists():
        return env
    for cand in (Path.home() / "dev/govern/target/release/govern-verify",):
        if cand.exists():
            return str(cand)
    return shutil.which("govern-verify")


def _govern_available() -> bool:
    return _govern_binary() is not None


def govern_verify_sample(params: dict) -> dict:
    """Offline-verify a govern execution bundle. Today serves the vendored sample bundle
    (proves the capability + this host's verifier). A future POST/upload layer lets callers
    submit their own bundle. Gated on the govern-verify binary being present on the host."""
    binary = _govern_binary()
    bundle = _ASSETS / "govern-sample"
    try:
        r = subprocess.run([binary, str(bundle)], capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"product": "govern-bundle-verify", "verdict": "VERIFIER_ERROR",
                "detail": str(e)[:100]}
    return {"product": "govern-bundle-verify", "bundle": "vendored-sample",
            "verdict": "OFFLINE-VERIFIED" if r.returncode == 0 else "FAIL",
            "exit_code": r.returncode, "detail": (r.stdout or r.stderr).strip().splitlines()[-1:],
            "claims_verified": ["chain_integrity", "policy_attestation", "witness_binding",
                                "ed25519_signatures"] if r.returncode == 0 else []}


# ── on-chain settlement verification (Base USDC) — product AND self-check ───────────────

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc(rpc_url: str, method: str, params: list) -> dict | None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={**_UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("result")


def verify_usdc_tx(tx_hash: str, *, rpc_url: str = "https://mainnet.base.org",
                   usdc: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913") -> dict:
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
            transfers.append({"from": "0x" + lg["topics"][1][-40:],
                              "to": "0x" + lg["topics"][2][-40:],
                              "value_minor_units": int(lg["data"], 16)})
    return {"product": "onchain-tx-verify", "tx": tx_hash,
            "verdict": "SUCCESS" if ok and transfers else ("REVERTED" if not ok else "NO_USDC_TRANSFER"),
            "block": int(receipt["blockNumber"], 16), "usdc_transfers": transfers, "rpc": rpc_url}


# ── the catalog ─────────────────────────────────────────────────────────────────────

CATALOG: dict[str, Product] = {
    "fx": Product(
        "Zero-spread FX conversion + disclosure between any two ECB currencies "
        "(canonically hashed, spread=0bps).",
        ecb_available, fx_convert, "?from=USD&to=JPY&amount=<minor units>"),
    "qswap": Product(
        "Measured LLM backend-selection matrices (fidelity + swap latency/memory across "
        "llama.cpp/mlx/candle on Apple Silicon).",
        _qswap_available, qswap_matrix, "?matrix=fidelity|swap|both"),
    "audit-verify": Product(
        "Verify a caller-submitted hash-chained audit log against its signed anchor "
        "(Ed25519/HMAC + chain integrity).",
        _keystone_available, keystone_verify, "?data=<base64 json {records,anchor,key?}>"),
    "verify-tx": Product(
        "Independent on-chain verification of a Base USDC transfer (receipt + decoded "
        "Transfer log).",
        lambda: True, lambda p: verify_usdc_tx((p.get("tx") or "")), "?tx=0x<64-hex>"),
    "govern-verify": Product(
        "Offline verification of a govern execution bundle (chain/policy/witness/signatures). "
        "Requires the govern-verify binary on this host.",
        _govern_available, govern_verify_sample, ""),
}


def catalog_summary() -> dict:
    return {name: {"description": p.description, "available": p.available(),
                   "params": p.needs} for name, p in CATALOG.items()}
