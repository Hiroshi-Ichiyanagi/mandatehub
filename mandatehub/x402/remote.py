"""
x402/remote.py — 実際の x402 ファシリテーター（例 Base 上の CDP）を叩く v1 クライアント。

/verify・/settle に {x402Version, paymentPayload, paymentRequirements} を POST する。
標準ライブラリ（urllib）のみ。セキュリティ要件（docs/X402.md §5）を焼き込む：
  - https 強制（localhost 例外のみ）、TLS 検証は無効化しない
  - /verify・/settle でのクロスホスト・リダイレクトを拒否
  - 署名/ペイロードは bearer 秘密：例外・ログに一切載せない（redaction）
  - 非 2xx・不正 JSON・ネットワーク断は例外化し、決して success 扱いしない
  - 未知キー（v2 の amount/extensions）や error/errorReason 揺れを許容

実際の CDP 認証ヘッダは header_hook（endpoint, body -> extra headers）で差し込む
（CDP 固有のヘッダ仕様は未確認のため既定 None）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from mandatehub.x402.wire import (
    FacilitatorSettleResult,
    FacilitatorVerifyResult,
    X402PaymentPayload,
    X402PaymentRequirements,
)


class FacilitatorError(RuntimeError):
    """ファシリテーター呼び出しの失敗（秘密は含めない）。"""


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """ホストが変わるリダイレクトを拒否する（/verify・/settle の SSRF/漏洩対策）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new = urlsplit(newurl)
        if new.netloc != urlsplit(req.full_url).netloc:
            raise FacilitatorError("cross-host redirect refused")
        # netloc carries no scheme, so a same-host https->http downgrade would otherwise
        # slip through and drop TLS (leaking header_hook auth headers in cleartext).
        if new.scheme != "https" and not _is_localhost(new.hostname):
            raise FacilitatorError("insecure (non-https) redirect target refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_localhost(host: str | None) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


# 既定 UA。urllib 既定の "Python-urllib/x.y" は公共 facilitator 前段の WAF
# （Cloudflare bot 対策）に 403 で弾かれる（x402.org /verify で実測、error 1010）。
_USER_AGENT = "mandatehub-x402/1 (+https://github.com/Hiroshi-Ichiyanagi/mandatehub)"


class RemoteFacilitatorAdapter:
    """x402 v1 ファシリテーター・クライアント。"""

    def __init__(
        self,
        facilitator_url: str,
        *,
        x402_version: int = 1,
        network: str = "base-sepolia",
        timeout: float = 30.0,
        header_hook: Callable[[str, dict[str, Any]], dict[str, str]] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if x402_version != 1:
            raise NotImplementedError("only x402 v1 is supported in this build")
        parsed = urlsplit(facilitator_url)
        if parsed.scheme != "https" and not _is_localhost(parsed.hostname):
            raise FacilitatorError("facilitator URL must be https (localhost may use http for tests)")
        self._url = facilitator_url.rstrip("/")
        self._network = network
        self._timeout = timeout
        self._header_hook = header_hook
        self._opener = opener or urllib.request.build_opener(_NoCrossHostRedirect())

    @property
    def network(self) -> str:
        return self._network

    def _post(
        self,
        endpoint: str,
        payment_payload: X402PaymentPayload,
        requirements: X402PaymentRequirements,
    ) -> dict[str, Any]:
        body = {
            "x402Version": 1,
            "paymentPayload": payment_payload.to_wire(),
            "paymentRequirements": requirements.to_wire(),
        }
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
        if self._header_hook is not None:
            headers.update(self._header_hook(endpoint, body))
        req = urllib.request.Request(f"{self._url}/{endpoint}", data=data, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # ステータスのみ。ペイロード/署名は絶対に載せない（redaction）
            raise FacilitatorError(f"facilitator /{endpoint} returned HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise FacilitatorError(f"facilitator /{endpoint} network error: {e.reason!r}") from None
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise FacilitatorError(f"facilitator /{endpoint} returned malformed JSON") from None
        if not isinstance(parsed, dict):
            raise FacilitatorError(f"facilitator /{endpoint} returned non-object JSON")
        return parsed

    def verify(
        self, payment_payload: X402PaymentPayload, requirements: X402PaymentRequirements
    ) -> FacilitatorVerifyResult:
        return FacilitatorVerifyResult.from_wire(self._post("verify", payment_payload, requirements))

    def settle(
        self, payment_payload: X402PaymentPayload, requirements: X402PaymentRequirements
    ) -> FacilitatorSettleResult:
        return FacilitatorSettleResult.from_wire(self._post("settle", payment_payload, requirements))
