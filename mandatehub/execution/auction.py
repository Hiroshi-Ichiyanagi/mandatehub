"""
execution/auction.py — ソルバーオークション（最良執行の決定論的選定）。

複数のソルバーが同じインテントの充足を入札し、利用者にとって最良の入札を
決定論的に選ぶ。同点は solver_id 昇順。敗者も記録するため、第三者は
「勝者が他のすべての開示入札に勝っていた」ことをオフラインで検証できる。

スコープの正直さ：このオークションが証明するのは「開示された候補集合の中で
最良」であって「利用可能なすべての見積りの中で最良」ではない。抑圧された
入札や、全ソルバーが共謀して一律に過少報告した場合は、ライブ見積りオラクル
なしにはオフラインで検出できない（proofs / docs に明記）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

OBJ_MIN_COST = "MIN_COST"  # fill_cost_cents 最小（利用者の支払い最小）
OBJ_MAX_OUT = "MAX_OUT"  # quoted_out_cents 最大（利用者の受取最大）


@dataclass(frozen=True)
class SolverBid:
    """1 ソルバーの入札。"""

    solver_id: str
    intent_id: str
    fill_cost_cents: int  # C_in 建て、小さいほど良い
    quoted_out_cents: int  # C_out 建て、大きいほど良い
    gas_cents: int
    valid: bool = True


@dataclass(frozen=True)
class AuctionOutcome:
    """オークションの結果。winner=None は有効入札なし（NO_WINNING_BID）。"""

    intent_id: str
    objective: str  # "MIN_COST" | "MAX_OUT"
    winner: SolverBid | None
    reference: SolverBid | None  # 2 番手（"no worse than" ベンチマーク）
    losers: tuple[SolverBid, ...]  # 勝者以外の有効入札（決定論的順）
    invalid: tuple[SolverBid, ...]  # 無効入札（記録のみ）


def run_auction(
    bids: Sequence[SolverBid],
    *,
    objective: str = OBJ_MIN_COST,
) -> AuctionOutcome:
    """入札集合から決定論的に勝者を選ぶ。

    MIN_COST: fill_cost_cents 昇順、同点 solver_id 昇順。
    MAX_OUT:  quoted_out_cents 降順、同点 solver_id 昇順。
    有効入札が無ければ winner=None。
    """
    intent_id = bids[0].intent_id if bids else ""

    if objective == OBJ_MIN_COST:
        key = lambda b: (b.fill_cost_cents, b.solver_id)  # noqa: E731
    elif objective == OBJ_MAX_OUT:
        key = lambda b: (-b.quoted_out_cents, b.solver_id)  # noqa: E731
    else:
        raise ValueError(f"unknown objective: {objective}")

    valid = sorted((b for b in bids if b.valid), key=key)
    invalid = tuple(b for b in bids if not b.valid)

    if not valid:
        return AuctionOutcome(
            intent_id=intent_id,
            objective=objective,
            winner=None,
            reference=None,
            losers=(),
            invalid=invalid,
        )

    winner = valid[0]
    reference = valid[1] if len(valid) > 1 else None
    losers = tuple(valid[1:])
    return AuctionOutcome(
        intent_id=intent_id,
        objective=objective,
        winner=winner,
        reference=reference,
        losers=losers,
        invalid=invalid,
    )
