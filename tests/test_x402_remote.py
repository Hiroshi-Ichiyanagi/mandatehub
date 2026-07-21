"""tests/test_x402_remote.py — Phase 2: real x402 v1 client (exact/EVM) against stubs.

No real network, no real keys: an injected fake opener stands in for the facilitator and a
StubSigner stands in for crypto. Covers the spec's §6.3 test plan.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    ExactEvmPayloadBuilder,
    FacilitatorError,
    RemoteFacilitatorAdapter,
    StubSigner,
    X402PaymentPayload,
    X402PaymentRequirements,
    build_transfer_with_authorization,
    decode_x_payment,
    encode_x_payment,
)
from mandatehub.x402.remote import _NoCrossHostRedirect
from mandatehub.x402.wire import FacilitatorSettleResult

FIXED_NONCE = bytes.fromhex("f3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480")


def _reqs():
    return X402PaymentRequirements(
        scheme="exact", network="base-sepolia", max_amount_required="10000", asset=BASE_SEPOLIA_USDC,
        pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C", resource="https://api.example/data",
        max_timeout_seconds=60, extra={"name": "USDC", "version": "2"},
    )


def _payload(signer=None):
    b = ExactEvmPayloadBuilder(signer or StubSigner(), network="base-sepolia", clock=lambda: 1740672149, nonce_source=lambda: FIXED_NONCE)
    return b.build(_reqs())


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StubOpener:
    """Injectable urllib opener: maps endpoint -> dict|bytes|Exception."""

    def __init__(self):
        self.responses: dict = {}
        self.calls: list = []

    def set(self, endpoint, resp):
        self.responses[endpoint] = resp
        return self

    def open(self, req, timeout=None):
        endpoint = req.full_url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, json.loads(req.data.decode()), dict(req.headers)))
        r = self.responses.get(endpoint)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, (bytes, bytearray)):
            return _Resp(bytes(r))
        return _Resp(json.dumps(r).encode())


class TestPayloadBuild:
    def test_golden_structure(self):
        w = _payload().to_wire()
        assert w["x402Version"] == 1 and w["scheme"] == "exact" and w["network"] == "base-sepolia"
        a = w["payload"]["authorization"]
        assert a["from"] == StubSigner().address
        assert a["to"] == "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"
        assert a["value"] == "10000"
        assert a["validAfter"] == "1740672089" and a["validBefore"] == "1740672209"
        assert a["nonce"] == "0x" + FIXED_NONCE.hex() and len(a["nonce"]) == 66
        assert w["payload"]["signature"].startswith("0x") and len(w["payload"]["signature"]) == 132  # 65 bytes

    def test_requires_domain_extra(self):
        reqs = X402PaymentRequirements(scheme="exact", network="base-sepolia", max_amount_required="1",
                                       asset=BASE_SEPOLIA_USDC, pay_to="0xabc", resource="r", max_timeout_seconds=60, extra=None)
        with pytest.raises(ValueError):
            ExactEvmPayloadBuilder(StubSigner()).build(reqs)

    def test_nonce_unique_across_builds(self):
        b = ExactEvmPayloadBuilder(StubSigner())  # real CSPRNG nonce
        nonces = {b.build(_reqs()).payload.authorization.nonce for _ in range(50)}
        assert len(nonces) == 50

    def test_network_mismatch_rejected(self):
        # builder configured for base-sepolia, requirements say base -> fail-closed
        reqs = X402PaymentRequirements(scheme="exact", network="base", max_amount_required="1",
                                       asset=BASE_SEPOLIA_USDC, pay_to="0xabc", resource="r",
                                       max_timeout_seconds=60, extra={"name": "USDC", "version": "2"})
        with pytest.raises(ValueError):
            ExactEvmPayloadBuilder(StubSigner(), network="base-sepolia").build(reqs)


class TestCodec:
    def test_standard_base64_roundtrip(self):
        p = _payload()
        hdr = encode_x_payment(p)
        # standard alphabet (not urlsafe): url-safe would use - or _
        assert "-" not in hdr and "_" not in hdr
        assert decode_x_payment(hdr).to_wire() == p.to_wire()


class TestEip712:
    def test_domain_and_types(self):
        td = build_transfer_with_authorization(_payload().payload.authorization, domain_name="USDC", domain_version="2", chain_id=84532, verifying_contract=BASE_SEPOLIA_USDC)
        assert td["domain"] == {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": BASE_SEPOLIA_USDC}
        assert td["primaryType"] == "TransferWithAuthorization"
        assert [f["name"] for f in td["types"]["TransferWithAuthorization"]] == ["from", "to", "value", "validAfter", "validBefore", "nonce"]
        assert isinstance(td["message"]["value"], int) and isinstance(td["message"]["nonce"], bytes)


class TestVerifyWiring:
    def _adapter(self, resp):
        return RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=StubOpener().set("verify", resp))

    def test_valid(self):
        v = self._adapter({"isValid": True, "invalidReason": None, "payer": "0x857b"}).verify(_payload(), _reqs())
        assert v.is_valid and v.payer == "0x857b"

    def test_invalid_with_reason(self):
        v = self._adapter({"isValid": False, "invalidReason": "insufficient_funds", "payer": "0x857b"}).verify(_payload(), _reqs())
        assert not v.is_valid and v.invalid_reason == "insufficient_funds"

    def test_posts_v1_envelope(self):
        op = StubOpener().set("verify", {"isValid": True})
        RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).verify(_payload(), _reqs())
        endpoint, body, _hdrs = op.calls[0]
        assert endpoint == "verify" and body["x402Version"] == 1
        assert set(body.keys()) == {"x402Version", "paymentPayload", "paymentRequirements"}

    def test_sends_real_user_agent_overridable_by_hook(self):
        # 既定の Python-urllib UA は公共 facilitator の WAF に 403 で弾かれる（x402.org 実測）。
        op = StubOpener().set("verify", {"isValid": True})
        RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).verify(_payload(), _reqs())
        hdrs = {k.lower(): v for k, v in op.calls[0][2].items()}
        assert hdrs.get("user-agent", "").startswith("mandatehub-x402/")
        op2 = StubOpener().set("verify", {"isValid": True})
        RemoteFacilitatorAdapter(
            "https://x402.org/facilitator", opener=op2,
            header_hook=lambda e, b: {"User-Agent": "custom/9"},
        ).verify(_payload(), _reqs())
        hdrs2 = {k.lower(): v for k, v in op2.calls[0][2].items()}
        assert hdrs2.get("user-agent") == "custom/9"


class TestSettleWiring:
    def _adapter(self, resp):
        return RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=StubOpener().set("settle", resp))

    def test_success(self):
        s = self._adapter({"success": True, "errorReason": None, "payer": "0x857b", "transaction": "0xdead", "network": "base-sepolia"}).settle(_payload(), _reqs())
        assert s.success and s.transaction == "0xdead" and s.network == "base-sepolia"

    def test_failure_empty_tx(self):
        s = self._adapter({"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": "base-sepolia"}).settle(_payload(), _reqs())
        assert not s.success and s.transaction == "" and s.error_reason == "insufficient_funds"

    def test_error_alias_and_unknown_reason(self):
        # v1 spec example used `error` instead of `errorReason`; tolerate it
        s = FacilitatorSettleResult.from_wire({"success": False, "error": "some_new_reason", "transaction": "", "network": "base-sepolia"})
        assert s.error_reason == "some_new_reason"  # unknown reason kept, not hard-failed

    def test_tolerates_v2_extra_keys(self):
        s = self._adapter({"success": True, "transaction": "0x1", "network": "base-sepolia", "amount": "10000", "extensions": {}}).settle(_payload(), _reqs())
        assert s.success


class TestSecurity:
    def test_https_required(self):
        with pytest.raises(FacilitatorError):
            RemoteFacilitatorAdapter("http://evil.example/facilitator")

    def test_localhost_http_allowed(self):
        RemoteFacilitatorAdapter("http://127.0.0.1:8080/facilitator")  # no raise (tests)

    def test_cross_host_redirect_refused(self):
        import urllib.request
        h = _NoCrossHostRedirect()
        req = urllib.request.Request("https://good.example/verify")
        with pytest.raises(FacilitatorError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/verify")

    def test_same_host_https_to_http_downgrade_refused(self):
        # same host but scheme downgrade https->http must be refused (would drop TLS)
        import urllib.request
        h = _NoCrossHostRedirect()
        req = urllib.request.Request("https://good.example/verify")
        with pytest.raises(FacilitatorError):
            h.redirect_request(req, None, 302, "Found", {}, "http://good.example/verify")

    def test_non_2xx_is_error_not_success(self):
        op = StubOpener().set("settle", urllib.error.HTTPError("https://x402.org/facilitator/settle", 429, "Too Many", {}, None))
        with pytest.raises(FacilitatorError) as ei:
            RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).settle(_payload(), _reqs())
        assert "429" in str(ei.value)

    def test_malformed_json_is_error(self):
        op = StubOpener().set("settle", b"<html>not json</html>")
        with pytest.raises(FacilitatorError):
            RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).settle(_payload(), _reqs())

    def test_secret_never_in_error(self):
        # the signature (bearer secret) must never appear in a raised error
        p = _payload()
        sig = p.payload.signature
        op = StubOpener().set("settle", urllib.error.HTTPError("https://x402.org/facilitator/settle", 500, "err", {}, None))
        with pytest.raises(FacilitatorError) as ei:
            RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).settle(p, _reqs())
        assert sig not in str(ei.value) and p.payload.authorization.nonce not in str(ei.value)

    def test_v2_guard(self):
        with pytest.raises(NotImplementedError):
            RemoteFacilitatorAdapter("https://x402.org/facilitator", x402_version=2)


class TestEvmExtra:
    def test_eth_account_signer_missing_extra(self):
        # eth-account is NOT installed in the test env -> constructing raises a clear error,
        # while importing the core succeeds without the extra.
        from mandatehub.signers import EthAccountSigner, MissingExtraError

        with pytest.raises(MissingExtraError):
            EthAccountSigner("0x" + "11" * 32)


class TestHttpErrorBodyParsing:
    """CDP は無効な支払いを HTTP 400 + 正規の verify/settle JSON で返す（実測）。"""

    def _http_error(self, code, body: dict | bytes):
        import io
        import urllib.error
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return urllib.error.HTTPError("https://x", code, "Bad Request", {}, io.BytesIO(raw))

    def test_verify_4xx_with_valid_envelope_is_parsed(self):
        op = StubOpener().set("verify", self._http_error(
            400, {"isValid": False, "invalidReason": "invalid_exact_evm_payload_signature",
                  "payer": "0x857b"}))
        v = RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).verify(_payload(), _reqs())
        assert not v.is_valid and v.invalid_reason == "invalid_exact_evm_payload_signature"

    def test_settle_4xx_with_valid_envelope_is_parsed(self):
        op = StubOpener().set("settle", self._http_error(
            400, {"success": False, "errorReason": "insufficient_funds", "payer": "0x857b",
                  "transaction": "", "network": "base-sepolia"}))
        s = RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).settle(_payload(), _reqs())
        assert not s.success and s.error_reason == "insufficient_funds"

    def test_4xx_with_non_envelope_body_still_raises(self):
        op = StubOpener().set("verify", self._http_error(400, {"errorType": "invalid_request"}))
        with pytest.raises(FacilitatorError, match="HTTP 400"):
            RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).verify(_payload(), _reqs())

    def test_4xx_with_malformed_body_still_raises(self):
        op = StubOpener().set("verify", self._http_error(500, b"<html>oops</html>"))
        with pytest.raises(FacilitatorError, match="HTTP 500"):
            RemoteFacilitatorAdapter("https://x402.org/facilitator", opener=op).verify(_payload(), _reqs())


class TestCdpHeaderHook:
    def test_missing_sdk_raises_helpful_error(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "cdp", None)
        monkeypatch.setitem(sys.modules, "cdp.auth", None)
        from mandatehub.signers import MissingCdpExtraError, cdp_header_hook
        with pytest.raises(MissingCdpExtraError, match=r"mandatehub\[cdp\]"):
            cdp_header_hook("id", "secret")

    def test_hook_binds_jwt_to_endpoint_path(self, monkeypatch):
        import sys
        import types
        calls = []

        class FakeJwtOptions:
            def __init__(self, **kw):
                self.kw = kw

        def fake_generate_jwt(options):
            calls.append(options.kw)
            return "FAKEJWT"

        fake_auth = types.SimpleNamespace(JwtOptions=FakeJwtOptions, generate_jwt=fake_generate_jwt)
        fake_cdp = types.ModuleType("cdp"); fake_cdp.auth = fake_auth
        monkeypatch.setitem(sys.modules, "cdp", fake_cdp)
        monkeypatch.setitem(sys.modules, "cdp.auth", fake_auth)

        from mandatehub.signers import cdp_header_hook
        hook = cdp_header_hook("kid", "ksec")
        hdrs = hook("verify", {})
        assert hdrs == {"Authorization": "Bearer FAKEJWT"}
        assert calls[0]["request_method"] == "POST"
        assert calls[0]["request_host"] == "api.cdp.coinbase.com"
        assert calls[0]["request_path"] == "/platform/v2/x402/verify"
        hook("settle", {})
        assert calls[1]["request_path"] == "/platform/v2/x402/settle"
        # 鍵はヘッダに一切載らない
        assert "ksec" not in json.dumps(hdrs)


class TestV2WireCompat:
    def test_from_wire_accepts_v2_amount_and_caip2(self):
        # x402 v2 challenge shape: `amount` + CAIP-2 network (as served by CDP-listed resources)
        d = {"scheme": "exact", "network": "eip155:8453", "amount": "10000",
             "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "payTo": "0x" + "11" * 20,
             "resource": "https://x/quote-v2", "maxTimeoutSeconds": 60,
             "extra": {"name": "USD Coin", "version": "2"}}
        r = X402PaymentRequirements.from_wire(d)
        assert r.max_amount_required == "10000" and r.network == "eip155:8453"
        from mandatehub.x402.eip712 import chain_id_for
        assert chain_id_for("eip155:8453") == 8453
        assert chain_id_for("eip155:84532") == 84532

    def test_from_wire_missing_amount_is_clear_error(self):
        with pytest.raises(KeyError, match="maxAmountRequired/amount"):
            X402PaymentRequirements.from_wire({"scheme": "exact", "network": "base",
                "asset": "0x", "payTo": "0x", "resource": "r", "maxTimeoutSeconds": 60})
