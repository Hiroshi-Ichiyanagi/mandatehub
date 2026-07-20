"""
x402/facilitator.py — mandatehub を「委任枠 gate + 証明」レイヤとした x402 ファシリテーター。

x402 のファシリテーターは /verify（支払い可否）と /settle（オンチェーン実行）を提供する。
mandatehub 版はそれを踏襲しつつ：
  - verify : 委任枠（budget / policy / session-key / window / nonce）に照らした副作用なし判定
  - settle : SettlementAdapter に委譲して決済を確定し、ProofOfMandate を添えて返す

SettlementAdapter を差し替えることで、決済実行先を切り替えられる：
  - LedgerSettlementAdapter（既定）: mandatehub 元帳に記帳（モック/自己完結。実マネー無し）
  - 将来: 実際の x402 ファシリテーター（Coinbase CDP 等）へ委譲するオンチェーンアダプタ

決済実行そのもの（オンチェーン）は mandatehub のコア範囲外。ここが担保するのは
「委任枠内で認可され、正しく記帳・証明される」ことである。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mandatehub.core.types import Currency, Money
from mandatehub.intent.mandate import IntentSettlementEngine
from mandatehub.intent.proofs import ProofOfMandateGenerator
from mandatehub.x402.types import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)

DEFAULT_NETWORK = "mandatehub-ledger"


def _currency_from_code(code: str) -> Currency:
    for c in Currency:
        if c.code == code:
            return c
    raise ValueError(f"unknown asset/currency code: {code}")


class SettlementAdapter(Protocol):
    """決済実行の抽象。verify 済みの支払いを確定し (success, tx_id, reason) を返す。"""

    def settle(
        self,
        engine: IntentSettlementEngine,
        requirements: PaymentRequirements,
        payload: PaymentPayload,
        *,
        at: datetime,
    ) -> tuple[bool, str, str]: ...


class LedgerSettlementAdapter:
    """既定アダプタ：mandatehub 元帳に委任枠 gate 付きで記帳する（自己完結・実マネー無し）。"""

    def settle(
        self,
        engine: IntentSettlementEngine,
        requirements: PaymentRequirements,
        payload: PaymentPayload,
        *,
        at: datetime,
    ) -> tuple[bool, str, str]:
        currency = _currency_from_code(requirements.asset)
        res = engine.settle_intent(
            mandate_id=requirements.mandate_id,
            intent_id=payload.intent_id,
            payee_account_id=requirements.pay_to,
            amount=Money(cents=payload.amount_cents, currency=currency),
            purpose=requirements.purpose,
            at=at,
            nonce=payload.nonce,
        )
        return res.is_settled, (res.transaction_id or ""), res.reason


class Facilitator:
    """x402 互換のファシリテーター（verify / settle）。"""

    def __init__(
        self,
        engine: IntentSettlementEngine,
        *,
        network: str = DEFAULT_NETWORK,
        settlement_adapter: SettlementAdapter | None = None,
        supported_schemes: tuple[str, ...] = ("exact",),
    ) -> None:
        self._engine = engine
        self._network = network
        self._adapter: SettlementAdapter = settlement_adapter or LedgerSettlementAdapter()
        self._supported_schemes = supported_schemes

    @property
    def network(self) -> str:
        return self._network

    def _reject_pair(self, requirements: PaymentRequirements, payload: PaymentPayload) -> str | None:
        if requirements.scheme not in self._supported_schemes or payload.scheme not in self._supported_schemes:
            return "UNSUPPORTED_SCHEME"
        if requirements.network != self._network or payload.network != self._network:
            return "UNSUPPORTED_NETWORK"
        if payload.amount_cents > requirements.max_amount_required_cents:
            return "AMOUNT_EXCEEDS_REQUIRED"
        try:
            _currency_from_code(requirements.asset)
        except ValueError:
            return "UNSUPPORTED_ASSET"
        return None

    def verify(
        self, requirements: PaymentRequirements, payload: PaymentPayload, *, at: datetime
    ) -> VerifyResponse:
        """支払い可否を委任枠に照らして判定する（元帳は変更しない）。"""
        bad = self._reject_pair(requirements, payload)
        if bad is not None:
            return VerifyResponse(is_valid=False, reason=bad, payer=payload.payer, remaining_cents=0)
        currency = _currency_from_code(requirements.asset)
        ok, reason, remaining = self._engine.preauthorize(
            mandate_id=requirements.mandate_id,
            intent_id=payload.intent_id,
            payee_account_id=requirements.pay_to,
            amount=Money(cents=payload.amount_cents, currency=currency),
            purpose=requirements.purpose,
            at=at,
            nonce=payload.nonce,
        )
        return VerifyResponse(is_valid=ok, reason=reason, payer=payload.payer, remaining_cents=remaining)

    def settle(
        self, requirements: PaymentRequirements, payload: PaymentPayload, *, at: datetime
    ) -> SettleResponse:
        """支払いを確定し、ProofOfMandate を添えて返す。"""
        bad = self._reject_pair(requirements, payload)
        if bad is not None:
            return SettleResponse(
                success=False, network=self._network, transaction="", reason=bad, payer=payload.payer, proof=None
            )
        success, tx_id, reason = self._adapter.settle(self._engine, requirements, payload, at=at)
        proof = None
        if success:
            p, _tree = ProofOfMandateGenerator(self._engine).generate(requirements.mandate_id, snapshot_at=at)
            proof = p.to_public_summary()
        return SettleResponse(
            success=success,
            network=self._network,
            transaction=tx_id,
            reason="OK" if success else reason,
            payer=payload.payer,
            proof=proof,
        )
