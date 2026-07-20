"""
x402 — mandatehub を委任枠 gate + 証明レイヤとした x402 互換ファシリテーター（HTTP 402）。

x402（Coinbase の HTTP ネイティブ支払い規約）の役割・語彙・facilitator の verify/settle を
踏襲する。決済実行は SettlementAdapter で差し替え可能：既定は自己完結の元帳記帳（実マネー無し）、
将来は実際の x402 ファシリテーター（例 Base 上の USDC）へ委譲するアダプタを差し込む。
"""

from mandatehub.x402.facilitator import (
    DEFAULT_NETWORK,
    Facilitator,
    LedgerSettlementAdapter,
    SettlementAdapter,
)
from mandatehub.x402.http402 import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    decode_payload,
    decode_requirements,
    decode_settle_response,
    encode_payload,
    encode_requirements,
    encode_settle_response,
    serve_once,
)
from mandatehub.x402.types import (
    SCHEME_EXACT,
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)

__all__ = [
    "Facilitator",
    "SettlementAdapter",
    "LedgerSettlementAdapter",
    "DEFAULT_NETWORK",
    "PaymentRequirements",
    "PaymentPayload",
    "VerifyResponse",
    "SettleResponse",
    "SCHEME_EXACT",
    "serve_once",
    "encode_requirements",
    "decode_requirements",
    "encode_payload",
    "decode_payload",
    "encode_settle_response",
    "decode_settle_response",
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_PAYMENT_SIGNATURE",
    "HEADER_PAYMENT_RESPONSE",
]
