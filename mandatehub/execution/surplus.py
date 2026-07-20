"""
execution/surplus.py — サープラス（価格改善益 + アビトラージ益）の整数厳密な分配。

執行結果が利用者の指値を上回ったとき、その差額（surplus）をポリシーに従って
分配する：ガス補填（オフトップ）→ 利用者リベート → リファラー → オペレーター
マージン（唯一の残差受領先、丸め誤差をすべて吸収）。

厳密性は二重に担保される：
  1. compute_split が total() == surplus を assert（浮動小数点を一切使わない）。
  2. 分配は 1 つの balanced Transaction として記帳され、Transaction.__post_init__ が
     不均衡を拒否するため、丸め漏れのある分配はそもそも記帳できない（構造的強制）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from mandatehub.core.ledger import TransactionBuilder
from mandatehub.core.types import Currency, Money, TransactionStatus


class SplitPolicyError(ValueError):
    """分配ポリシーが不正なときに送出される（SPLIT_POLICY_INVALID）。"""


@dataclass(frozen=True)
class SurplusSplitPolicy:
    """サープラス分配ポリシー。bps の合計はちょうど 10000 でなければならない。"""

    user_rebate_bps: int  # >= 0
    operator_margin_bps: int  # >= 0（唯一の残差受領先）
    referrer_bps: int = 0  # >= 0
    gas_reimbursement_cents: int = 0

    def __post_init__(self) -> None:
        for name, v in (
            ("user_rebate_bps", self.user_rebate_bps),
            ("operator_margin_bps", self.operator_margin_bps),
            ("referrer_bps", self.referrer_bps),
            ("gas_reimbursement_cents", self.gas_reimbursement_cents),
        ):
            if not isinstance(v, int) or v < 0:
                raise SplitPolicyError(f"{name} must be a non-negative int, got {v!r}")
        total_bps = self.user_rebate_bps + self.operator_margin_bps + self.referrer_bps
        if total_bps != 10_000:
            raise SplitPolicyError(
                f"bps must sum to exactly 10000 (user+operator+referrer), got {total_bps}"
            )


@dataclass(frozen=True)
class SplitAllocation:
    """1 件のサープラス分配結果（整数 cents）。"""

    surplus_cents: int
    gas_cents: int
    user_rebate_cents: int
    operator_margin_cents: int
    referrer_cents: int

    def total(self) -> int:
        return (
            self.gas_cents
            + self.user_rebate_cents
            + self.operator_margin_cents
            + self.referrer_cents
        )


def compute_split(surplus_cents: int, policy: SurplusSplitPolicy) -> SplitAllocation:
    """サープラスをポリシーに従って整数厳密に分配する。

    ガスはオフトップで min(gas, surplus) にクランプ（利用者はサープラスを超えて
    負担しない）。残余を bps で按分し、オペレーターマージンが残差を吸収する。
    total() == surplus を保証（浮動小数点なし）。
    """
    if surplus_cents < 0:
        raise SplitPolicyError(f"surplus_cents must be >= 0, got {surplus_cents}")

    gas = min(policy.gas_reimbursement_cents, surplus_cents)  # defense-in-depth clamp
    distributable = surplus_cents - gas
    user_rebate = distributable * policy.user_rebate_bps // 10_000  # floor
    referrer = distributable * policy.referrer_bps // 10_000  # floor
    operator_margin = distributable - user_rebate - referrer  # 残差（丸めを吸収）

    alloc = SplitAllocation(
        surplus_cents=surplus_cents,
        gas_cents=gas,
        user_rebate_cents=user_rebate,
        operator_margin_cents=operator_margin,
        referrer_cents=referrer,
    )
    assert alloc.gas_cents >= 0
    assert alloc.user_rebate_cents >= 0
    assert alloc.operator_margin_cents >= 0
    assert alloc.referrer_cents >= 0
    assert alloc.total() == surplus_cents, "split must be integer-exact"
    return alloc


def post_surplus_split(
    ledger,
    *,
    surplus_source_account_id: str,
    allocation: SplitAllocation,
    accounts,
    currency: Currency,
    initiator_id: str,
    at: datetime,
    metadata: Sequence[tuple[str, str]] = (),
):
    """スタンドアロン経路：surplus_source からサープラスを分配先へ振り分ける。

    1 つの balanced Transaction として記帳する（SETTLED, settled_at=at）。
    bridge 経路はこれを使わず自前で 1 tx を記帳する（二重記帳防止）。
    """
    b = TransactionBuilder("SURPLUS_SPLIT", initiator_id, initiated_at=at)
    b.add_entry(surplus_source_account_id, Money(cents=-allocation.total(), currency=currency))
    if allocation.gas_cents:
        b.add_entry(accounts.gas_account_id, Money(cents=allocation.gas_cents, currency=currency))
    if allocation.user_rebate_cents:
        b.add_entry(
            accounts.user_rebate_account_id,
            Money(cents=allocation.user_rebate_cents, currency=currency),
        )
    if allocation.operator_margin_cents:
        b.add_entry(
            accounts.operator_margin_account_id,
            Money(cents=allocation.operator_margin_cents, currency=currency),
        )
    if allocation.referrer_cents:
        if accounts.referrer_account_id is None:
            raise SplitPolicyError("referrer allocation is non-zero but referrer account is None")
        b.add_entry(
            accounts.referrer_account_id,
            Money(cents=allocation.referrer_cents, currency=currency),
        )
    for k, v in metadata:
        b.with_metadata(k, v)
    tx = b.build(status=TransactionStatus.SETTLED, settled_at=at)
    ledger.post(tx)  # SETTLED で構築済みなので settle() は不要
    return tx
