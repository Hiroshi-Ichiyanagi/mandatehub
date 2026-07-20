"""
execution/routing.py — 決定論的なルート選択（③ の最安・最良経路計算）。

複数プール／会場をまたぐ候補ルートを、見積り出力（整数 cents）だけで比較し、
決定論的に最良を選ぶ。ライブチェーンには一切アクセスしない — 見積りは明示入力。
浮動小数点は使わず、整数の全順序で順位付けする（同点は route_id 昇順）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 目的関数
OBJ_MAX_NET_OUT = "MAX_NET_OUT"  # net_out_cents 最大化
OBJ_MIN_COST = "MIN_COST"  # fill_cost_cents 最小化


@dataclass(frozen=True)
class ExecutionVenue:
    """執行会場（DEX / プール群 / OTC など）の識別子。"""

    venue_id: str
    label: str


@dataclass(frozen=True)
class Pool:
    """1 つの流動性プール（in_currency -> out_currency の交換ができる）。"""

    pool_id: str
    venue_id: str
    in_currency_code: str
    out_currency_code: str


@dataclass(frozen=True)
class RouteQuote:
    """候補ルートの見積り。すべて整数 cents。"""

    route_id: str
    hops: tuple[str, ...]  # 経由する pool_id の列
    in_currency_code: str
    out_currency_code: str
    in_amount_cents: int
    gross_out_amount_cents: int
    route_fee_cents: int  # out 通貨建ての整数手数料

    @property
    def net_out_cents(self) -> int:
        return self.gross_out_amount_cents - self.route_fee_cents

    @property
    def fill_cost_cents(self) -> int:
        """入力コスト（Model A のコスト枠組み）。"""
        return self.in_amount_cents


@dataclass(frozen=True)
class RouteSelection:
    """ルート選択の結果。winner が最良、reference は 2 番手（単一候補なら None）。"""

    winner: RouteQuote
    reference: RouteQuote | None
    ranked: tuple[RouteQuote, ...]  # winner を先頭にした全順位


def select_best_route(
    quotes: Sequence[RouteQuote],
    *,
    objective: str = OBJ_MAX_NET_OUT,
) -> RouteSelection:
    """候補ルートから決定論的に最良を選ぶ。

    MAX_NET_OUT: net_out_cents 最大、同点は route_id 昇順。
    MIN_COST:    fill_cost_cents 最小、同点は route_id 昇順。
    空リストは ValueError。
    """
    if not quotes:
        raise ValueError("select_best_route requires at least one quote")

    if objective == OBJ_MAX_NET_OUT:
        key = lambda q: (-q.net_out_cents, q.route_id)  # noqa: E731
    elif objective == OBJ_MIN_COST:
        key = lambda q: (q.fill_cost_cents, q.route_id)  # noqa: E731
    else:
        raise ValueError(f"unknown objective: {objective}")

    ranked = tuple(sorted(quotes, key=key))
    winner = ranked[0]
    reference = ranked[1] if len(ranked) > 1 else None
    return RouteSelection(winner=winner, reference=reference, ranked=ranked)
