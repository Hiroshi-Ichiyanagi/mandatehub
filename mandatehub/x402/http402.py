"""
x402/http402.py — HTTP 402 の運搬層（ヘッダ符号化 + 1 リクエストの 402 フロー）。

x402 のヘッダ流儀を踏襲する：
  - PAYMENT-REQUIRED  : 402 応答に載る base64(JSON) の PaymentRequirements
  - PAYMENT-SIGNATURE : リクエストに載る base64(JSON) の PaymentPayload
  - PAYMENT-RESPONSE  : 200 応答に載る base64(JSON) の決済結果

`serve_once` は 1 リクエストの 402 フローを純関数として実装する（ソケット不要でテスト可能）。
実サーバ（stdlib http.server）は examples/x402_facilitator.py がこれを包む。標準ライブラリのみ。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Callable

from mandatehub.x402.facilitator import Facilitator
from mandatehub.x402.types import PaymentPayload, PaymentRequirements, SettleResponse

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"


def _b64_encode(obj: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _b64_decode(header_value: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(header_value.encode()).decode())


def encode_requirements(requirements: PaymentRequirements) -> str:
    return _b64_encode({"x402Version": 1, "accepts": [requirements.to_dict()]})


def decode_requirements(header_value: str) -> PaymentRequirements:
    d = _b64_decode(header_value)
    accepts = d.get("accepts") or [d]
    return PaymentRequirements.from_dict(accepts[0])


def encode_payload(payload: PaymentPayload) -> str:
    return _b64_encode(payload.to_dict())


def decode_payload(header_value: str) -> PaymentPayload:
    return PaymentPayload.from_dict(_b64_decode(header_value))


def encode_settle_response(resp: SettleResponse) -> str:
    return _b64_encode(resp.to_dict())


def decode_settle_response(header_value: str) -> dict[str, Any]:
    return _b64_decode(header_value)


def serve_once(
    facilitator: Facilitator,
    requirements: PaymentRequirements,
    request_headers: dict[str, str],
    resource_fn: Callable[[], Any],
    *,
    at: datetime,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """1 リクエストの 402 フローを処理する。

    Returns: (status_code, body, response_headers)。
      - 支払いヘッダなし          -> 402 + PAYMENT-REQUIRED
      - 支払いあり & 委任枠内      -> 200 + resource + PAYMENT-RESPONSE
      - 支払いあり & 却下          -> 402 + PAYMENT-REQUIRED（reason 付き）
    """
    # ヘッダは大文字小文字を無視して探す
    lower = {k.lower(): v for k, v in request_headers.items()}
    sig = lower.get(HEADER_PAYMENT_SIGNATURE.lower())

    if not sig:
        return (
            402,
            {"error": "payment required", "accepts": [requirements.to_dict()]},
            {HEADER_PAYMENT_REQUIRED: encode_requirements(requirements)},
        )

    payload = decode_payload(sig)
    settle = facilitator.settle(requirements, payload, at=at)
    if not settle.success:
        return (
            402,
            {"error": "payment rejected", "reason": settle.reason, "accepts": [requirements.to_dict()]},
            {HEADER_PAYMENT_REQUIRED: encode_requirements(requirements)},
        )

    body = resource_fn()
    return (200, body, {HEADER_PAYMENT_RESPONSE: encode_settle_response(settle)})
