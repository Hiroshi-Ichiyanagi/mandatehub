"""Wave-3 products (deploy/local): CVE snapshot, gas oracle, URL liveness, content attestation.

All offline: network is stubbed; the SSRF guard is exercised against non-routable targets
that never require a real connection.
"""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "local"


def _load(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    import netfetch
    import products
    importlib.reload(netfetch)
    importlib.reload(products)
    return products, netfetch


def test_netfetch_ssrf_guard(monkeypatch):
    """safe_fetch refuses non-public / non-http(s) targets with FetchError (never connects)."""
    _, netfetch = _load(monkeypatch)
    bad = ["http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
           "http://10.0.0.1/", "http://192.168.1.1/", "http://[::1]/", "http://0.0.0.0/",
           "ftp://example.com/", "file:///etc/passwd", "not-a-url"]
    for u in bad:
        try:
            netfetch.safe_fetch(u, timeout=3)
            raise AssertionError(f"SSRF leak: {u} was not refused")
        except netfetch.FetchError:
            pass  # expected


def test_gas_oracle_eip1559_deterministic(monkeypatch):
    """Next base fee is the EXACT EIP-1559 update (reproducible), artifact self-hashes,
    and the prediction is labeled not-financial-advice."""
    P, _ = _load(monkeypatch)
    assert P._next_base_fee(100, 15_000_000, 30_000_000) == 100      # at target -> unchanged
    assert P._next_base_fee(100, 30_000_000, 30_000_000) == 112      # full -> +12.5%
    assert P._next_base_fee(800, 0, 30_000_000) == 700               # empty -> -12.5%

    monkeypatch.setattr(P, "_rpc", lambda url, m, pr: {
        "number": "0x10", "hash": "0xabc", "timestamp": "0x64",
        "baseFeePerGas": hex(50_000_000), "gasUsed": hex(20_000_000), "gasLimit": hex(30_000_000)})
    g = P.gas_oracle({})
    assert g["product"] == "base-gas-oracle" and g["as_of_block"] == 16
    assert g["estimate_next_block"]["method"] == "eip1559-deterministic"
    assert "not financial advice" in g["disclaimer"]
    assert g["artifact_sha256"] == P._canonical_hash(
        {k: v for k, v in g.items() if k != "artifact_sha256"})
    assert P._gas_available() is True


def test_cve_snapshot_validation_and_pinning(monkeypatch):
    """CVE snapshot rejects bad ecosystems/packages for free and hash-pins OSV's response."""
    P, _ = _load(monkeypatch)
    assert "ecosystem must be" in P.cve_snapshot({"ecosystem": "BadEco", "package": "x"})["error"]
    assert "package" in P.cve_snapshot({"ecosystem": "PyPI", "package": ""})["error"]

    import urllib.request

    def fake_open(req, timeout=0):
        body = json.dumps({"vulns": [{"id": "GHSA-xxxx", "summary": "boom",
            "modified": "2026-01-01", "aliases": ["CVE-2026-1"],
            "severity": [{"type": "CVSS_V3", "score": "9.8"}]}]}).encode()
        return io.BytesIO(body)
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    c = P.cve_snapshot({"ecosystem": "PyPI", "package": "requests", "version": "2.31.0"})
    assert c["vulnerability_count"] == 1 and c["vulnerability_ids"] == ["GHSA-xxxx"]
    assert c["query"] == {"ecosystem": "PyPI", "package": "requests", "version": "2.31.0"}
    assert "not our own completeness" in c["attestation"]
    assert c["artifact_sha256"]


def test_url_products_precheck_and_attestation(monkeypatch):
    """url_precheck refuses malformed input for free (pre-charge); url_check /
    content_attestation handle 2xx, non-2xx, and SSRF refusal without raising post-charge."""
    P, netfetch = _load(monkeypatch)

    # free pre-charge refusals (network-free)
    assert P.url_precheck({"url": ""})[0] == 400
    assert P.url_precheck({"url": "ftp://x/"})[0] == 400
    assert P.url_precheck({"url": "http://x" * 400})[0] == 400
    assert P.url_precheck({"url": "https://example.com/ok"}) is None

    monkeypatch.setattr(P, "_rpc", lambda url, m, pr: {
        "number": "0x10", "hash": "0xabc", "timestamp": "0x64",
        "baseFeePerGas": "0x0", "gasUsed": "0x0", "gasLimit": "0x1"})
    ok = {"url": "u", "host": "example.com", "status": 200, "reason": "OK",
          "headers": {"content-type": "text/html"}, "body_sha256": "deadbeef",
          "body_bytes": 10, "truncated": False}
    monkeypatch.setattr(netfetch, "safe_fetch", lambda u, **k: ok)
    uc = P.url_check({"url": "https://example.com"})
    assert uc["status_code"] == 200 and uc["reachable"] and uc["content_sha256"] == "deadbeef"
    ca = P.content_attestation({"url": "https://example.com"})
    assert ca["attested"] is True and ca["anchor"]["block_number"] == 16
    assert "NOT their truthfulness" in ca["attestation"]

    # non-2xx: still a paid, honest result — attested=False with the observed status
    monkeypatch.setattr(netfetch, "safe_fetch",
                        lambda u, **k: {**ok, "status": 404, "reason": "NF", "body_sha256": "abc"})
    ca2 = P.content_attestation({"url": "https://example.com/missing"})
    assert ca2["attested"] is False and ca2["observed_status"] == 404

    # SSRF refusal inside build: returns a refused dict, never raises after charge
    def boom(u, **k):
        raise netfetch.FetchError("nope")
    monkeypatch.setattr(netfetch, "safe_fetch", boom)
    assert P.url_check({"url": "https://x"})["refused"] is True
    assert P.content_attestation({"url": "https://x"})["refused"] is True


def test_availability_probes_are_cached(monkeypatch):
    """Unpaid catalog renders (/, agents.json) call available() per product — those probes must
    NOT amplify into repeated outbound OSV/RPC requests. One probe per TTL window, pass or fail."""
    P, _ = _load(monkeypatch)
    calls = {"gas": 0, "osv": 0}

    monkeypatch.setattr(P, "_latest_block", lambda rpc=None: calls.__setitem__("gas", calls["gas"] + 1) or {"number": 1})
    monkeypatch.setattr(P, "_osv_probe", lambda: calls.__setitem__("osv", calls["osv"] + 1) or True)
    P._avail_cache.clear()
    for _ in range(10):
        assert P._gas_available() is True
        assert P._osv_available() is True
    assert calls == {"gas": 1, "osv": 1}          # cached within TTL

    # failures are negative-cached too (no hammering a down dependency)
    P._avail_cache.clear()
    def boom(*a, **k):
        calls["gas"] += 1
        raise RuntimeError("down")
    monkeypatch.setattr(P, "_latest_block", boom)
    calls["gas"] = 0
    for _ in range(10):
        assert P._gas_available() is False
    assert calls["gas"] == 1


def test_osv_probe_treats_4xx_as_reachable(monkeypatch):
    """api.osv.dev answers 404 on its root; urllib raises HTTPError on 4xx. The probe must read
    that as 'host up' (found live: cve-snapshot was 503 on the VPS because of this)."""
    P, _ = _load(monkeypatch)
    import urllib.request
    from urllib.error import HTTPError

    def raise_404(req, timeout=0):
        raise HTTPError("https://api.osv.dev/", 404, "Not Found", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", raise_404)
    assert P._osv_probe() is True

    def raise_503(req, timeout=0):
        raise HTTPError("https://api.osv.dev/", 503, "Unavailable", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", raise_503)
    assert P._osv_probe() is False


def test_netfetch_privileged_ports_and_cve_version_cap(monkeypatch):
    """Privileged ports other than 80/443 are refused (no probing SSH/SMTP/DBs via us);
    cve-snapshot caps the version length."""
    P, netfetch = _load(monkeypatch)
    for u in ("http://example.com:22/", "https://example.com:25/", "http://example.com:993/"):
        try:
            netfetch.safe_fetch(u, timeout=3)
            raise AssertionError(f"privileged port allowed: {u}")
        except netfetch.FetchError as e:
            assert "privileged port" in str(e)
    assert "version too long" in P.cve_snapshot(
        {"ecosystem": "PyPI", "package": "x", "version": "v" * 101})["error"]


def test_gas_priority_fees_from_feehistory(monkeypatch):
    """Priority-fee tiers are the observed p10/p50/p90 tips from eth_feeHistory (real data),
    not static constants; degrades to 'unavailable' without dropping the base-fee estimate."""
    P, _ = _load(monkeypatch)

    def fake_rpc(url, method, params):
        if method == "eth_getBlockByNumber":
            return {"number": "0x10", "hash": "0xabc", "timestamp": "0x64",
                    "baseFeePerGas": hex(50_000_000), "gasUsed": hex(20_000_000),
                    "gasLimit": hex(30_000_000)}
        if method == "eth_feeHistory":
            # two blocks, [p10,p50,p90] tips in wei
            return {"reward": [[hex(1_000_000), hex(5_000_000), hex(20_000_000)],
                               [hex(1_000_000), hex(5_000_000), hex(20_000_000)]]}
        return None
    monkeypatch.setattr(P, "_rpc", fake_rpc)
    g = P.gas_oracle({})
    pf = g["suggested_priority_fee"]
    assert pf["method"] == "eth_feeHistory p10/p50/p90" and pf["window_blocks"] == 2
    assert pf["economy_gwei"] == 0.001 and pf["standard_gwei"] == 0.005 and pf["fast_gwei"] == 0.02
    assert g["artifact_sha256"]

    # feeHistory empty -> priority marked unavailable, base-fee estimate still present
    monkeypatch.setattr(P, "_rpc", lambda u, m, p: (
        {"number": "0x10", "hash": "0xabc", "timestamp": "0x64", "baseFeePerGas": "0x1",
         "gasUsed": "0x0", "gasLimit": "0x2"} if m == "eth_getBlockByNumber" else None))
    g2 = P.gas_oracle({})
    assert g2["suggested_priority_fee"]["method"] == "unavailable"
    assert g2["estimate_next_block"]["method"] == "eip1559-deterministic"


def test_attestation_signature_roundtrip(tmp_path, monkeypatch):
    """content_attestation carries a publicly-verifiable EIP-191 signature when a key is set;
    the signer recovers to the published attest_signer_address(). Unsigned without a key."""
    P, netfetch = _load(monkeypatch)
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception:
        import pytest
        pytest.skip("eth-account not installed in this environment")

    # deterministic throwaway key (test-only)
    key = "0x" + "11" * 32
    kf = tmp_path / "attest.key"
    kf.write_text(key)
    monkeypatch.setenv("MANDATEHUB_ATTEST_KEY_FILE", str(kf))
    P._attest_cache.clear()

    addr = P.attest_signer_address()
    assert addr == Account.from_key(key).address

    monkeypatch.setattr(P, "_rpc", lambda u, m, p: {
        "number": "0x10", "hash": "0xabc", "timestamp": "0x64",
        "baseFeePerGas": "0x0", "gasUsed": "0x0", "gasLimit": "0x1"})
    monkeypatch.setattr(netfetch, "safe_fetch", lambda u, **k: {
        "url": u, "host": "h", "status": 200, "reason": "OK", "headers": {},
        "body_sha256": "beef", "body_bytes": 4, "truncated": False})
    a = P.content_attestation({"url": "https://example.com"})
    sig = a["operator_signature"]
    assert sig["scheme"] == "eip191-secp256k1" and sig["signer"] == addr
    # a third party recovers the signer from (digest, signature) with no trust in us
    recovered = Account.recover_message(
        encode_defunct(text=a["artifact_sha256"]), signature=sig["signature"])
    assert recovered == addr

    # no key -> unsigned, but attestation still returned
    monkeypatch.delenv("MANDATEHUB_ATTEST_KEY_FILE")
    P._attest_cache.clear()
    a2 = P.content_attestation({"url": "https://example.com"})
    assert "operator_signature" not in a2 and a2["attested"] is True
    assert P.attest_signer_address() is None


def test_committer_availability_gated_on_balance(monkeypatch):
    """The on-chain tier is available ONLY when the attestation address holds enough gas;
    the balance probe is cached (no per-render RPC hammering)."""
    P, _ = _load(monkeypatch)
    monkeypatch.setenv("MANDATEHUB_ATTEST_KEY_FILE", "/nonexistent")
    P._attest_cache.clear(); P._avail_cache.clear()
    assert P._committer_available() is False          # no key -> unavailable

    # with a key: gated purely on balance vs COMMIT_MIN_BALANCE_WEI
    import tempfile, os
    kf = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
    kf.write("0x" + "22" * 32); kf.close()
    monkeypatch.setenv("MANDATEHUB_ATTEST_KEY_FILE", kf.name)
    P._attest_cache.clear(); P._avail_cache.clear()
    try:
        from eth_account import Account  # noqa: F401
    except Exception:
        import pytest; pytest.skip("eth-account not installed")

    calls = {"n": 0}
    def rpc_rich(url, m, p):
        calls["n"] += 1
        return hex(P.COMMIT_MIN_BALANCE_WEI + 1) if m == "eth_getBalance" else None
    monkeypatch.setattr(P, "_rpc", rpc_rich)
    for _ in range(5):
        assert P._committer_available() is True
    assert calls["n"] == 1                              # cached within TTL

    P._avail_cache.clear()
    monkeypatch.setattr(P, "_rpc", lambda u, m, p: hex(P.COMMIT_MIN_BALANCE_WEI - 1))
    assert P._committer_available() is False           # underfunded -> unavailable
    os.unlink(kf.name)


def test_content_attestation_onchain_commit(tmp_path, monkeypatch):
    """On-chain tier builds/signs/broadcasts a Base tx whose calldata is 0x+artifact hash, polls
    the receipt, and reports the commitment; non-2xx targets commit nothing; broadcast failure is
    reported not raised."""
    P, netfetch = _load(monkeypatch)
    try:
        from eth_account import Account
    except Exception:
        import pytest; pytest.skip("eth-account not installed")

    kf = tmp_path / "attest.key"; kf.write_text("0x" + "33" * 32)
    monkeypatch.setenv("MANDATEHUB_ATTEST_KEY_FILE", str(kf))
    P._attest_cache.clear()
    addr = P.attest_signer_address()

    monkeypatch.setattr(P, "time", type("T", (), {
        "sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0.0)})())
    sent = {}
    def fake_rpc(url, m, params):
        if m == "eth_getBlockByNumber":
            return {"number": "0x10", "hash": "0xabc", "timestamp": "0x64",
                    "baseFeePerGas": hex(1_000_000), "gasUsed": "0x0", "gasLimit": "0x1"}
        if m == "eth_getTransactionCount":
            return "0x0"
        if m == "eth_sendRawTransaction":
            sent["raw"] = params[0]
            return "0x" + "ab" * 32
        if m == "eth_getTransactionReceipt":
            return {"blockNumber": "0x11", "status": "0x1"}
        return None
    monkeypatch.setattr(P, "_rpc", fake_rpc)
    monkeypatch.setattr(netfetch, "safe_fetch", lambda u, **k: {
        "url": u, "host": "h", "status": 200, "reason": "OK", "headers": {},
        "body_sha256": "cafe", "body_bytes": 4, "truncated": False})

    a = P.content_attestation_onchain({"url": "https://example.com"})
    c = a["onchain_commitment"]
    assert c["status"] == "mined" and c["block_number"] == 17
    assert c["tx_hash"] == "0x" + "ab" * 32 and c["committer"] == addr
    assert c["calldata"] == "0x" + a["artifact_sha256"]        # the hash IS the calldata
    assert sent["raw"].startswith("0x")                        # a real signed tx was broadcast
    # recover the signer of the raw tx to confirm it came from the committer address
    from eth_account import Account as _A
    assert _A.recover_transaction(sent["raw"]) == addr

    # non-2xx: nothing committed
    monkeypatch.setattr(netfetch, "safe_fetch",
                        lambda u, **k: {"url": u, "host": "h", "status": 500, "reason": "err",
                                        "headers": {}, "body_sha256": "x", "body_bytes": 1,
                                        "truncated": False})
    a2 = P.content_attestation_onchain({"url": "https://example.com"})
    assert a2["attested"] is False and "onchain_commitment" not in a2

    # broadcast failure is reported, not raised (paid caller still gets the signed attestation)
    def rpc_no_broadcast(url, m, params):
        if m == "eth_sendRawTransaction":
            return None
        return fake_rpc(url, m, params)
    monkeypatch.setattr(P, "_rpc", rpc_no_broadcast)
    monkeypatch.setattr(netfetch, "safe_fetch", lambda u, **k: {
        "url": u, "host": "h", "status": 200, "reason": "OK", "headers": {},
        "body_sha256": "cafe", "body_bytes": 4, "truncated": False})
    a3 = P.content_attestation_onchain({"url": "https://example.com"})
    assert a3["onchain_commitment"]["status"] == "broadcast_failed"
    assert "operator_signature" in a3                          # signed attestation still delivered


def test_catalog_wave3_registered(monkeypatch):
    """The four new products are registered with the right gates and prechecks."""
    P, _ = _load(monkeypatch)
    for name in ("cve-snapshot", "gas-oracle", "url-liveness", "content-attestation"):
        assert name in P.CATALOG, name
    assert P.CATALOG["url-liveness"].precheck is P.url_precheck
    assert P.CATALOG["content-attestation"].precheck is P.url_precheck
    assert P.CATALOG["url-liveness"].available() is True   # per-URL result, no feed dependency
    # the operator route dispatches precheck before settlement
    src = (DEPLOY / "operator.py").read_text()
    assert "prod.precheck(params)" in src
