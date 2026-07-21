"""
x402/wire.py — 実際の x402 **v1** ワイヤ型（EVM `exact` スキーム）と base64/JSON コーデック。

これは Phase 1 の mandatehub 内部型（x402/types.py, cents ベースのモック用）とは別物で、
本物の x402 ファシリテーター（例 Base 上の Coinbase CDP）が話す v1 フォーマットを厳密に写す。
金額は最小単位の整数を文字列で持つ（USDC は 6 桁）。アドレスは EVM の 0x アドレス。

出典に基づく確定事項（docs/X402.md 参照）：
  - client→server ヘッダ `X-PAYMENT` = base64(標準・パディング有) の PaymentPayload JSON
  - server→client ヘッダ `X-PAYMENT-RESPONSE` = base64 の SettleResponse JSON
  - /verify・/settle のリクエストは {x402Version, paymentPayload, paymentRequirements}
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EIP3009Authorization:
    """EIP-3009 transferWithAuthorization のパラメータ（全て文字列でワイヤに載る）。"""

    from_: str
    to: str
    value: str  # 最小単位の整数を文字列で
    valid_after: str
    valid_before: str
    nonce: str  # 0x 前置の 32byte hex

    def to_wire(self) -> dict[str, Any]:
        return {
            "from": self.from_,
            "to": self.to,
            "value": self.value,
            "validAfter": self.valid_after,
            "validBefore": self.valid_before,
            "nonce": self.nonce,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "EIP3009Authorization":
        return cls(
            from_=d["from"],
            to=d["to"],
            value=str(d["value"]),
            valid_after=str(d["validAfter"]),
            valid_before=str(d["validBefore"]),
            nonce=d["nonce"],
        )


@dataclass(frozen=True)
class ExactEvmPayload:
    """PaymentPayload.payload の中身（exact/EVM）：署名 + authorization。"""

    signature: str | None
    authorization: EIP3009Authorization

    def to_wire(self) -> dict[str, Any]:
        return {"signature": self.signature, "authorization": self.authorization.to_wire()}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "ExactEvmPayload":
        return cls(signature=d.get("signature"), authorization=EIP3009Authorization.from_wire(d["authorization"]))


@dataclass(frozen=True)
class X402PaymentPayload:
    """X-PAYMENT ヘッダの中身（v1 exact/EVM）。"""

    scheme: str
    network: str
    payload: ExactEvmPayload
    x402_version: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "x402Version": self.x402_version,
            "scheme": self.scheme,
            "network": self.network,
            "payload": self.payload.to_wire(),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "X402PaymentPayload":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            payload=ExactEvmPayload.from_wire(d["payload"]),
            x402_version=int(d.get("x402Version", 1)),
        )


@dataclass(frozen=True)
class X402PaymentRequirements:
    """resource server が提示する支払い条件（v1、camelCase, exclude_none）。"""

    scheme: str
    network: str
    max_amount_required: str  # 最小単位・文字列
    asset: str  # ERC-20 コントラクトアドレス
    pay_to: str  # 受取 EVM アドレス
    resource: str
    max_timeout_seconds: int
    description: str | None = None
    mime_type: str | None = None
    output_schema: Any | None = None
    extra: dict[str, Any] | None = None  # exact/EIP-3009 では EIP-712 domain {name, version}

    def to_wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": self.max_amount_required,
            "asset": self.asset,
            "payTo": self.pay_to,
            "resource": self.resource,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "description": self.description,
            "mimeType": self.mime_type,
            "outputSchema": self.output_schema,
            "extra": self.extra,
        }
        return {k: v for k, v in d.items() if v is not None}  # exclude_none

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "X402PaymentRequirements":
        # v1 は maxAmountRequired、x402 v2 は amount。どちらも無ければ明確に落とす。
        amount = d.get("maxAmountRequired", d.get("amount"))
        if amount is None:
            raise KeyError("payment requirements missing maxAmountRequired/amount")
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            max_amount_required=str(amount),
            asset=d["asset"],
            pay_to=d["payTo"],
            resource=d["resource"],
            max_timeout_seconds=int(d["maxTimeoutSeconds"]),
            description=d.get("description"),
            mime_type=d.get("mimeType"),
            output_schema=d.get("outputSchema"),
            extra=d.get("extra"),
        )


@dataclass(frozen=True)
class FacilitatorVerifyResult:
    """/verify の応答。"""

    is_valid: bool
    invalid_reason: str | None
    payer: str | None

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "FacilitatorVerifyResult":
        return cls(
            is_valid=bool(d["isValid"]),
            invalid_reason=d.get("invalidReason"),
            payer=d.get("payer"),
        )


@dataclass(frozen=True)
class FacilitatorSettleResult:
    """/settle の応答。失敗時 transaction は空文字。error/errorReason 両対応。"""

    success: bool
    error_reason: str | None
    payer: str | None
    transaction: str
    network: str

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "FacilitatorSettleResult":
        return cls(
            success=bool(d["success"]),
            # v1 spec の例で error/errorReason が揺れていたため両対応（errorReason 優先）
            error_reason=d.get("errorReason") if d.get("errorReason") is not None else d.get("error"),
            payer=d.get("payer"),
            transaction=d.get("transaction", "") or "",
            network=d.get("network", "") or "",
        )


# ---------- base64 / JSON コーデック（標準 base64・パディング有、url-safe ではない） ----------


def _b64(obj: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _unb64(header_value: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(header_value.encode("ascii")).decode("utf-8"))


def encode_x_payment(payload: X402PaymentPayload) -> str:
    """X-PAYMENT ヘッダ値を作る。"""
    return _b64(payload.to_wire())


def decode_x_payment(header_value: str) -> X402PaymentPayload:
    return X402PaymentPayload.from_wire(_unb64(header_value))


def encode_x_payment_response(settle: FacilitatorSettleResult) -> str:
    """X-PAYMENT-RESPONSE ヘッダ値を作る。"""
    return _b64(
        {
            "success": settle.success,
            "errorReason": settle.error_reason,
            "payer": settle.payer,
            "transaction": settle.transaction,
            "network": settle.network,
        }
    )


def decode_x_payment_response(header_value: str) -> dict[str, Any]:
    return _unb64(header_value)
