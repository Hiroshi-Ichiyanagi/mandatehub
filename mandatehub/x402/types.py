"""
x402/types.py — x402 互換の支払い型（PaymentRequirements / PaymentPayload / 応答）。

Coinbase の x402 の役割・語彙に合わせる：resource server が 402 で PaymentRequirements を
返し、client が PaymentPayload を提示し、facilitator が verify/settle する。mandatehub 固有の
拡張として mandate_id / purpose を持ち、決済は委任枠（budget/policy/session-key）で gate される。

すべて整数 cents。ヘッダ運搬用に JSON 変換を提供する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEME_EXACT = "exact"  # x402 の最初のスキーム（指定額をちょうど支払う）


@dataclass(frozen=True)
class PaymentRequirements:
    """resource server が要求する支払い条件（402 応答の中身）。"""

    scheme: str  # "exact"
    network: str  # 決済ネットワーク識別子（例 "mandatehub-ledger", 将来 "base-sepolia"）
    max_amount_required_cents: int
    resource: str  # 課金対象リソースの URL/ID
    description: str
    pay_to: str  # 受取先（元帳の payee account_id、将来はオンチェーンアドレス）
    asset: str  # 通貨コード（例 "USDC"）
    mandate_id: str  # mandatehub 拡張：この支払いを認可する委任枠
    purpose: str  # 委任枠の purpose_code
    max_timeout_seconds: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": self.max_amount_required_cents,
            "resource": self.resource,
            "description": self.description,
            "payTo": self.pay_to,
            "asset": self.asset,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": {"mandateId": self.mandate_id, "purpose": self.purpose},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaymentRequirements":
        extra = d.get("extra") or {}
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            max_amount_required_cents=int(d["maxAmountRequired"]),
            resource=d["resource"],
            description=d.get("description", ""),
            pay_to=d["payTo"],
            asset=d["asset"],
            mandate_id=extra["mandateId"],
            purpose=extra["purpose"],
            max_timeout_seconds=int(d.get("maxTimeoutSeconds", 60)),
        )


@dataclass(frozen=True)
class PaymentPayload:
    """client が提示する支払いペイロード（x402 の署名付きペイロードに相当）。"""

    scheme: str
    network: str
    intent_id: str  # 冪等キー（x402 の nonce に相当）
    amount_cents: int  # client が支払う額（<= max_amount_required）
    payer: str  # 支払人（自律エージェント／プリンシパル）識別子
    nonce: int | None = None  # mandate の replay 保護用（任意）

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "intentId": self.intent_id,
            "amount": self.amount_cents,
            "payer": self.payer,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaymentPayload":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            intent_id=d["intentId"],
            amount_cents=int(d["amount"]),
            payer=d.get("payer", ""),
            nonce=(int(d["nonce"]) if d.get("nonce") is not None else None),
        )


@dataclass(frozen=True)
class VerifyResponse:
    """facilitator /verify の応答（元帳は変更しない）。"""

    is_valid: bool
    reason: str
    payer: str
    remaining_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "isValid": self.is_valid,
            "reason": self.reason,
            "payer": self.payer,
            "remaining": self.remaining_cents,
        }


@dataclass(frozen=True)
class SettleResponse:
    """facilitator /settle の応答（決済実行結果 + 委任枠証明）。"""

    success: bool
    network: str
    transaction: str
    reason: str
    payer: str
    proof: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "network": self.network,
            "transaction": self.transaction,
            "reason": self.reason,
            "payer": self.payer,
            "proof": self.proof,
        }
