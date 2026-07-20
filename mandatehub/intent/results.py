"""
intent/results.py — 決済判断の結果 / 要求の値オブジェクト（葉モジュール）。

mandate.py を import しないため cycle を作らない。execution の証明型は
参照するが、これは許可された依存方向（intent -> execution）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mandatehub.core.types import Money
from mandatehub.execution.proofs import ProofOfBestExecution, ProofOfSurplusRecapture
from mandatehub.execution.surplus import SplitAllocation


@dataclass(frozen=True)
class IntentSettlementResult:
    """1 件のインテント決済判断（成立 or 却下）の記録。"""

    intent_id: str
    mandate_id: str
    decision: str  # "SETTLED" | "DENIED"
    amount: Money
    purpose: str
    payee_account_id: str
    reason: str
    decided_at: datetime
    remaining_after_cents: int
    transaction_id: str | None = None
    audit_sequence: int | None = None

    @property
    def is_settled(self) -> bool:
        return self.decision == "SETTLED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "mandate_id": self.mandate_id,
            "decision": self.decision,
            "amount_cents": self.amount.cents,
            "currency": self.amount.currency.code,
            "purpose": self.purpose,
            "payee_account_id": self.payee_account_id,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "remaining_after_cents": self.remaining_after_cents,
            "transaction_id": self.transaction_id,
            "audit_sequence": self.audit_sequence,
        }


@dataclass(frozen=True)
class IntentRequest:
    """バッチ決済に渡す 1 件のインテント要求。"""

    intent_id: str
    payee_account_id: str
    amount: Money
    purpose: str
    nonce: int | None = None


@dataclass(frozen=True)
class BatchSettlementResult:
    """バッチ決済（全件成立 or 全件却下）の結果。"""

    mandate_id: str
    decision: str  # "SETTLED" | "DENIED"（バッチ全体）
    reason: str  # "OK" または "<最初の失敗理由>@<intent_id>"
    transaction_id: str | None
    per_intent: tuple[IntentSettlementResult, ...]
    audit_sequence: int | None
    remaining_after_cents: int


@dataclass(frozen=True)
class AuctionSettlementResult:
    """best-execution 経由の決済結果（決済 + 分配 + 2 つの証明）。"""

    settlement: IntentSettlementResult
    executed_cost_cents: int | None
    split: SplitAllocation | None
    best_execution: ProofOfBestExecution | None
    surplus_recapture: ProofOfSurplusRecapture | None
    reason: str
