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
    # Optional CHEAP, NETWORK-FREE input check run BEFORE settlement. Returns None to proceed,
    # or (http_code, body) to refuse for free (so a caller is not charged for invalid input).
    precheck: Callable[[dict], "tuple[int, dict] | None"] | None = None


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


ECB_RETRY_BACKOFF_SECONDS = 120  # after a failed fetch, don't hammer ECB every request


def _ecb_data(now: float | None = None) -> dict | None:
    t = time.time() if now is None else now
    stale = _ecb_cache["data"] is None or t - _ecb_cache["at"] > ECB_TTL_SECONDS
    backing_off = t - _ecb_cache.get("last_attempt", -1e18) < ECB_RETRY_BACKOFF_SECONDS
    if stale and not backing_off:
        _ecb_cache["last_attempt"] = t   # negative-cache: stamp the attempt regardless
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
    if not str(amount_raw).isdigit() or not (0 < int(amount_raw) <= 10 ** 15):
        return {"error": "amount must be a positive integer in minor units (<= 1e15)"}
    from decimal import Decimal, ROUND_HALF_UP
    amount = int(amount_raw)  # minor units of `from`
    rate_from, rate_to = Decimal(per_eur[frm]), Decimal(per_eur[to])
    cross = rate_to / rate_from
    target = int((Decimal(amount) * cross).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
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


# ── 5. govern offline bundle verification (vendored PYTHON pyverify — runs anywhere) ────

_PYVERIFY = _ASSETS / "pyverify"


def _pyverify_available() -> bool:
    return (_PYVERIFY / "__main__.py").exists() and (_PYVERIFY / "genuine.bundle").exists()


def _last_line_safe(out: str) -> str:
    import re
    line = out.strip().splitlines()[-1:][0][:200] if out.strip() else ""
    return re.sub(r"(/[\w./-]+/)([\w.-]+)", r"\2", line)  # drop absolute dir paths


def _run_pyverify(bundle_dir: Path) -> dict:
    import os
    env = {**os.environ, "PYTHONPATH": str(_ASSETS)}
    r = subprocess.run([sys.executable, "-m", "pyverify", str(bundle_dir)],
                       capture_output=True, text=True, timeout=60, env=env)
    return {"verdict": "OFFLINE-VERIFIED" if r.returncode == 0 else "FAIL",
            "exit_code": r.returncode,
            "detail": _last_line_safe(r.stdout or r.stderr),
            "claims_checked": ["hash_chain", "ed25519_receipts", "witness_binding",
                               "sth_consistency"]}


def govern_verify(params: dict) -> dict:
    """Offline-verify a govern evidence bundle via the vendored pure-Python verifier.

    Modes: ?bundle=genuine|tampered (vendored demo bundles — proves the verifier PASSES a
    genuine bundle and FAILS a tampered one), or ?data=<base64 zip of a bundle dir, ≤256KB>
    to verify the caller's own bundle.
    """
    import io
    import tempfile
    import zipfile
    which = (params.get("bundle") or "").lower()
    if which in ("genuine", "tampered"):
        out = _run_pyverify(_PYVERIFY / f"{which}.bundle")
        out.update({"product": "govern-bundle-verify", "bundle": f"vendored-{which}"})
        return out
    raw = params.get("data")
    if not raw:
        return {"error": "pass ?bundle=genuine|tampered (demo) or "
                         "?data=<base64 zip of your bundle dir, ≤256KB>"}
    try:
        blob = base64.b64decode(raw)
        if len(blob) > 256 * 1024:
            return {"product": "govern-bundle-verify", "error": "bundle zip exceeds 256KB cap"}
        with tempfile.TemporaryDirectory(prefix="mh-bundle-") as td:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                infos = z.infolist()
                if len(infos) > 256:
                    return {"product": "govern-bundle-verify", "error": "too many zip entries"}
                if sum(zi.file_size for zi in infos) > 8 * 1024 * 1024:  # decompressed cap 8MB
                    return {"product": "govern-bundle-verify", "error": "bundle expands too large"}
                for zi in infos:                 # zip-slip guard
                    if zi.filename.startswith("/") or ".." in zi.filename:
                        return {"product": "govern-bundle-verify", "error": "unsafe zip paths"}
                z.extractall(td)
            root = Path(td)
            entries = [e for e in root.iterdir() if e.is_dir()]
            bundle = entries[0] if len(entries) == 1 and not (root / "manifest.json").exists() else root
            out = _run_pyverify(bundle)
            out.update({"product": "govern-bundle-verify", "bundle": "caller-submitted"})
            return out
    except Exception as e:
        return {"product": "govern-bundle-verify", "error": f"malformed zip: {type(e).__name__}"}


# ── 6. openunit — population-weighted unit of account (vendored, live re-verified) ─────

_OPENUNIT = _ASSETS / "openunit"


def _openunit_available() -> bool:
    return all((_OPENUNIT / f).exists() for f in ("openunit.py", "artifact.json", "spec.json"))


def openunit_value(params: dict) -> dict:
    if str(_OPENUNIT) not in sys.path:
        sys.path.insert(0, str(_OPENUNIT))
    import openunit as OU  # vendored, stdlib-only
    art = json.loads((_OPENUNIT / "artifact.json").read_text())
    spec = json.loads((_OPENUNIT / "spec.json").read_text())
    verified = bool(OU.verify_artifact(art, spec))   # re-verified LIVE on every sale
    body = {"product": "openunit-valuation",
            "value_usd": art["value_usd"], "numeraire": art["numeraire"],
            "method": f'{art["method"]} {art["method_version"]}',
            "weight_basis": art["weight_basis"], "vintage": art["weight_vintage_label"],
            "input_digest": art["input_digest"], "artifact_hash": art["artifact_hash"],
            "reverified_now": verified}
    body["artifact_sha256"] = _canonical_hash(body)
    return body


# ── 7. kairos — JP equities convergence scores (static snapshot, honest as-of) ─────────

_KAIROS = _ASSETS / "kairos" / "kcs-snapshot.json"


def _kairos_available() -> bool:
    return _KAIROS.exists()


def kairos_scores(params: dict) -> dict:
    snap = json.loads(_KAIROS.read_text())
    try:
        top_n = max(1, min(300, int(params.get("top") or 25)))
    except ValueError:
        top_n = 25
    body = {"product": "kairos-kcs-snapshot", "as_of": snap["as_of"],
            "staleness_note": "static research snapshot; NOT live market data, NOT advice",
            "method": snap["method"], "universe_size": snap["universe_size"],
            "top": snap["top"][:top_n]}
    body["artifact_sha256"] = _canonical_hash(body)
    return body


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


# ── block anchor + gas oracle (Base RPC) ──────────────────────────────────────────────

BASE_RPC = "https://mainnet.base.org"


def _latest_block(rpc_url: str = BASE_RPC) -> dict:
    """Latest Base block header (number, hash, timestamp, baseFeePerGas, gasUsed, gasLimit)."""
    b = _rpc(rpc_url, "eth_getBlockByNumber", ["latest", False])
    if not b:
        raise RuntimeError("no block from RPC")
    return {
        "number": int(b["number"], 16),
        "hash": b["hash"],
        "timestamp": int(b["timestamp"], 16),
        "base_fee_per_gas": int(b.get("baseFeePerGas", "0x0"), 16),
        "gas_used": int(b.get("gasUsed", "0x0"), 16),
        "gas_limit": int(b.get("gasLimit", "0x1"), 16),
    }


def _next_base_fee(base_fee: int, gas_used: int, gas_limit: int) -> int:
    """EIP-1559 next-block base fee — DETERMINISTIC from the current block, not a guess.
    target = gas_limit/2; fee moves at most 1/8 per block toward filling/emptying."""
    target = gas_limit // 2
    if target == 0:
        return base_fee
    if gas_used == target:
        return base_fee
    if gas_used > target:
        delta = max(1, base_fee * (gas_used - target) // target // 8)
        return base_fee + delta
    delta = base_fee * (target - gas_used) // target // 8
    return base_fee - delta


def gas_oracle(params: dict, *, rpc_url: str = BASE_RPC) -> dict:
    """Base gas conditions + a transparent, reproducible next-block estimate.

    The next base fee is the EXACT EIP-1559 computation from the current block (verifiable, not
    a forecast). The 'suggested' priority fees are a disclosed heuristic over the current base
    fee. This is operational cost data — NOT financial advice; the caller decides when to send."""
    blk = _latest_block(rpc_url)
    nxt = _next_base_fee(blk["base_fee_per_gas"], blk["gas_used"], blk["gas_limit"])
    congestion = round(blk["gas_used"] / blk["gas_limit"], 4) if blk["gas_limit"] else None
    gwei = lambda w: round(w / 1e9, 4)  # noqa: E731
    # priority-fee tiers: disclosed heuristic anchored to the (small) Base priority market
    tiers = {"economy_gwei": 0.001, "standard_gwei": 0.005, "fast_gwei": 0.02}
    body = {
        "product": "base-gas-oracle",
        "as_of_block": blk["number"],
        "block_hash": blk["hash"],
        "block_timestamp": blk["timestamp"],
        "network": "base-mainnet",
        "observed": {
            "base_fee_gwei": gwei(blk["base_fee_per_gas"]),
            "gas_used": blk["gas_used"], "gas_limit": blk["gas_limit"],
            "congestion_ratio": congestion,
        },
        "estimate_next_block": {
            "base_fee_gwei": gwei(nxt),
            "method": "eip1559-deterministic",
            "note": "exact EIP-1559 base-fee update from this block; reproducible on-chain",
        },
        "suggested_priority_fee": {**tiers,
            "method": "disclosed-heuristic",
            "max_fee_fast_gwei": gwei(nxt) + tiers["fast_gwei"]},
        "disclaimer": "operational gas estimate, not financial advice; you decide when to send",
    }
    body["artifact_sha256"] = _canonical_hash(body)
    return body


_avail_cache: dict = {}
AVAIL_TTL_SECONDS = 120   # same spirit as ECB_RETRY_BACKOFF: unpaid catalog renders must not
                          # be able to amplify into repeated outbound probes (OSV / RPC)


def _cached_avail(key: str, probe: Callable[[], bool], now: float | None = None) -> bool:
    t = time.time() if now is None else now
    hit = _avail_cache.get(key)
    if hit is not None and t - hit[1] < AVAIL_TTL_SECONDS:
        return hit[0]
    try:
        val = bool(probe())
    except Exception:
        val = False
    _avail_cache[key] = (val, t)
    return val


def _gas_available(rpc_url: str = BASE_RPC) -> bool:
    return _cached_avail("gas", lambda: _latest_block(rpc_url) is not None)


# ── CVE snapshot (OSV.dev — point-in-time, hash-pinned) ────────────────────────────────

OSV_URL = "https://api.osv.dev/v1/query"
_CVE_ECOSYSTEMS = {"PyPI", "npm", "Go", "crates.io", "Maven", "RubyGems", "NuGet", "Packagist"}


def cve_snapshot(params: dict) -> dict:
    """A point-in-time, hash-pinned snapshot of OSV.dev advisories for one package.

    Params: ?ecosystem=PyPI&package=requests[&version=2.31.0]. We attest 'this is what OSV
    returned at this time', hash-pinned — NOT a completeness or safety judgment of our own."""
    eco = (params.get("ecosystem") or "").strip()
    pkg = (params.get("package") or "").strip()
    ver = (params.get("version") or "").strip()
    if eco not in _CVE_ECOSYSTEMS:
        return {"product": "cve-snapshot", "error": f"ecosystem must be one of {sorted(_CVE_ECOSYSTEMS)}"}
    if not pkg or len(pkg) > 200:
        return {"product": "cve-snapshot", "error": "pass ?package=<name> (<=200 chars)"}
    if len(ver) > 100:
        return {"product": "cve-snapshot", "error": "version too long (<=100 chars)"}
    query: dict = {"package": {"name": pkg, "ecosystem": eco}}
    if ver:
        query["version"] = ver
    payload = json.dumps(query).encode()
    req = urllib.request.Request(OSV_URL, data=payload,
                                 headers={**_UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    vulns = data.get("vulns") or []
    ids = sorted(v.get("id", "") for v in vulns)
    summaries = [{"id": v.get("id"), "summary": (v.get("summary") or "")[:200],
                  "modified": v.get("modified"),
                  "aliases": sorted(v.get("aliases") or [])[:8],
                  "severity": v.get("severity") or []} for v in vulns]
    summaries.sort(key=lambda x: x["id"] or "")
    body = {
        "product": "cve-snapshot",
        "source": "osv.dev",
        "query": {"ecosystem": eco, "package": pkg, "version": ver or None},
        "vulnerability_count": len(ids),
        "vulnerability_ids": ids,
        "advisories": summaries,
        "attestation": "hash-pinned snapshot of OSV.dev's response; not our own completeness "
                       "or safety judgment",
    }
    body["artifact_sha256"] = _canonical_hash(body)
    return body


def _osv_probe() -> bool:
    with urllib.request.urlopen(urllib.request.Request(
            "https://api.osv.dev/", headers=_UA), timeout=8) as r:
        return r.status < 500


def _osv_available() -> bool:
    return _cached_avail("osv", _osv_probe)


# ── URL liveness / tamper monitor + content-existence attestation (SSRF-guarded) ───────

def url_precheck(params: dict) -> "tuple[int, dict] | None":
    """CHEAP, NETWORK-FREE format check run before settlement. Full SSRF validation (DNS + the
    real peer IP) happens later in netfetch.safe_fetch; this only rejects obviously-bad input
    for free so a caller is never charged for a malformed request."""
    from urllib.parse import urlsplit
    url = (params.get("url") or "").strip()
    if not url:
        return 400, {"error": "pass ?url=https://… (http/https, public host only)"}
    if len(url) > 2048:
        return 400, {"error": "url too long (<=2048 chars)"}
    p = urlsplit(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return 400, {"error": "url must be an absolute http(s) URL with a host"}
    return None


def url_check(params: dict) -> dict:
    """Liveness + tamper report for a caller-supplied URL: current status code and a sha256 of
    the body, so an agent can health-check / detect drift before depending on the resource.
    Charged for performing the check — 'unreachable' is a valid result. Malformed input is
    refused for free by url_precheck; an unsafe (non-public) target is refused here safely."""
    from netfetch import safe_fetch, FetchError
    try:
        r = safe_fetch((params.get("url") or "").strip())
    except FetchError as e:
        return {"product": "url-liveness", "refused": True, "reason": str(e)}
    body = {
        "product": "url-liveness",
        "url": r["url"], "host": r.get("host"),
        "status_code": r.get("status"),
        "reachable": r.get("status") is not None,
        "reason": r.get("reason"),
        "content_sha256": r.get("body_sha256"),
        "content_bytes": r.get("body_bytes"),
        "content_type": r.get("headers", {}).get("content-type"),
        "location": r.get("headers", {}).get("location"),
        "truncated": r.get("truncated", False),
        "note": "status + content hash as observed now; redirects are reported, not followed",
    }
    body["artifact_sha256"] = _canonical_hash(body)
    return body


def content_attestation(params: dict, *, rpc_url: str = BASE_RPC) -> dict:
    """Prove 'this content was observed at this time': fetch the URL, hash its bytes, and anchor
    to the latest Base block (block.timestamp = trustable on-chain time). Attests EXISTENCE /
    integrity of the observed bytes — NOT that the content is true. `attested` is true only for a
    2xx target. The block anchor requires RPC (gated by availability -> 503 before charge)."""
    from netfetch import safe_fetch, FetchError
    anchor = _latest_block(rpc_url)     # available() gated this; defensive if RPC blips
    try:
        r = safe_fetch((params.get("url") or "").strip())
    except FetchError as e:
        return {"product": "content-attestation", "refused": True, "reason": str(e)}
    status = r.get("status")
    attested = status is not None and 200 <= status < 300
    body = {
        "product": "content-attestation",
        "url": r["url"], "host": r.get("host"),
        "observed_status": status,
        "attested": attested,
        "content_sha256": r.get("body_sha256"),
        "content_bytes": r.get("body_bytes"),
        "content_type": r.get("headers", {}).get("content-type"),
        "truncated": r.get("truncated", False),
        "anchor": {"chain": "base-mainnet", "block_number": anchor["number"],
                   "block_hash": anchor["hash"], "block_timestamp": anchor["timestamp"]},
        "attestation": ("operator observed the above content bytes at/around this Base block; "
                        "attests existence & integrity of the bytes, NOT their truthfulness"
                        if attested else
                        "target was not 2xx; recorded observed status but no content is attested"),
    }
    body["artifact_sha256"] = _canonical_hash(body)
    return body


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
        "Offline verification of a govern evidence bundle (hash chain, Ed25519 receipts, "
        "witness binding, STH consistency) — pure-Python verifier; demo bundles or submit "
        "your own as a base64 zip.",
        _pyverify_available, govern_verify, "?bundle=genuine|tampered or ?data=<base64 zip>"),
    "openunit": Product(
        "openunit valuation — a deterministic, population-weighted unit of account "
        "(UN-WPP + WB-PPP vintages), artifact re-verified live on every sale.",
        _openunit_available, openunit_value, ""),
    "kairos": Product(
        "Kairos Convergence Scores for ~2000 JP equities (multi-pillar tailwind convergence; "
        "static research snapshot with explicit as-of; not advice).",
        _kairos_available, kairos_scores, "?top=1..300"),
    "cve-snapshot": Product(
        "Point-in-time, hash-pinned snapshot of OSV.dev advisories for a package — a trustworthy "
        "reference source for AI code audits (attests OSV's response, not our own judgment).",
        _osv_available, cve_snapshot, "?ecosystem=PyPI&package=<name>&version=<optional>"),
    "gas-oracle": Product(
        "Base gas conditions + a reproducible next-block base-fee estimate (exact EIP-1559) and "
        "disclosed priority-fee tiers, anchored to the current block. Operational data, not advice.",
        _gas_available, gas_oracle, ""),
    "url-liveness": Product(
        "Liveness + tamper report for a URL: current status code and a sha256 of the body, so an "
        "agent can health-check / detect drift before depending on an external resource.",
        lambda: True, url_check, "?url=<percent-encoded https URL, public host>", precheck=url_precheck),
    "content-attestation": Product(
        "Content-existence attestation: fetch a URL, hash its bytes, and anchor to the current "
        "Base block (on-chain time). Proves the content was observed at that time — not that it "
        "is true. `attested` is true only for a 2xx target.",
        _gas_available, content_attestation, "?url=<percent-encoded https URL, public host>", precheck=url_precheck),
}


def catalog_summary() -> dict:
    return {name: {"description": p.description, "available": p.available(),
                   "params": p.needs} for name, p in CATALOG.items()}
