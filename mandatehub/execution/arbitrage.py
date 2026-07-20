"""
execution/arbitrage.py — 循環アビトラージの検出と帰属（memo-only）。

見積りプールグラフ上で利益の出る閉路（cycle）を決定論的に検出し、その価値を
帰属・記録する。整数の床除算で伝播するため過大評価しない（保守的）。

会計上の決定（phantom-revenue の矛盾を解消）：スタンドアロンのグラフ・アビトラージは
**memo-only** — CyclicArbOpportunity を返し監査イベント arbitrage_detected を刻むが、
元帳には一切記帳しない（何も執行していないため。記帳すれば裏付けのない収益を
発生させ「執行者が支払能力を持つことの証明」に反する）。実際にルートが充足された
ときのみ、bridge 経由で VENUE_CLEARING を相手方として計上される。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mandatehub.transparency.audit_log import AuditLog


@dataclass(frozen=True)
class PoolEdge:
    """有向エッジ：from 通貨を in_amount 供給すると to 通貨が out_amount 得られる見積り。"""

    from_currency_code: str
    to_currency_code: str
    in_amount_cents: int
    out_amount_cents: int


@dataclass(frozen=True)
class PoolGraph:
    edges: tuple[PoolEdge, ...]


@dataclass(frozen=True)
class CyclicArbOpportunity:
    """利益の出る循環アビトラージ機会。"""

    cycle: tuple[str, ...]  # 通貨コードの閉路（先頭と末尾が start_currency）
    start_amount_cents: int
    end_amount_cents: int
    profit_cents: int  # end - start（> 0）
    edge_path: tuple[int, ...]  # PoolGraph.edges へのインデックス列


def find_best_arbitrage_cycle(
    graph: PoolGraph,
    *,
    start_currency: str,
    start_amount_cents: int,
    max_cycle_len: int = 4,
) -> CyclicArbOpportunity | None:
    """start_currency から始まり戻る単純閉路のうち、最大利益のものを返す。

    伝播は整数の床除算 out = amount * e.out // e.in（過大評価しない）。
    利益は end > start のときのみ。同利益なら edge_path が辞書順最小のものを選ぶ。
    利益の出る閉路が無ければ None。
    """
    if start_amount_cents <= 0 or max_cycle_len < 2:
        return None

    edges = graph.edges
    best: CyclicArbOpportunity | None = None

    def consider(path_edges: list[int], amount: int) -> None:
        nonlocal best
        profit = amount - start_amount_cents
        if profit <= 0:
            return
        edge_path = tuple(path_edges)
        cycle = [start_currency]
        for idx in path_edges:
            cycle.append(edges[idx].to_currency_code)
        cand = CyclicArbOpportunity(
            cycle=tuple(cycle),
            start_amount_cents=start_amount_cents,
            end_amount_cents=amount,
            profit_cents=profit,
            edge_path=edge_path,
        )
        if best is None or cand.profit_cents > best.profit_cents or (
            cand.profit_cents == best.profit_cents and cand.edge_path < best.edge_path
        ):
            best = cand

    def dfs(current_ccy: str, amount: int, path_edges: list[int], visited: frozenset[str]) -> None:
        if path_edges and current_ccy == start_currency:
            consider(path_edges, amount)
            return  # 単純閉路：閉じたら延長しない
        if len(path_edges) >= max_cycle_len:
            return
        for i, e in enumerate(edges):
            if e.from_currency_code != current_ccy:
                continue
            if e.in_amount_cents <= 0:
                continue
            nxt = e.to_currency_code
            if nxt != start_currency and nxt in visited:
                continue  # 中間通貨の再訪は不可（単純閉路）
            out = amount * e.out_amount_cents // e.in_amount_cents  # 整数床（過大評価しない）
            dfs(nxt, out, path_edges + [i], visited | {nxt})

    dfs(start_currency, start_amount_cents, [], frozenset({start_currency}))
    return best


def record_arbitrage_detection(
    audit_log: AuditLog,
    opportunity: CyclicArbOpportunity,
    *,
    at: datetime,
    detector_id: str = "arb_detector_v1",
):
    """検出したアビトラージ機会を監査ログに刻む（memo-only、元帳記帳なし）。"""
    payload: dict[str, Any] = {
        "cycle": list(opportunity.cycle),
        "start_amount_cents": opportunity.start_amount_cents,
        "end_amount_cents": opportunity.end_amount_cents,
        "profit_cents": opportunity.profit_cents,
        "edge_path": list(opportunity.edge_path),
        "detector_id": detector_id,
    }
    return audit_log.append("arbitrage_detected", payload, timestamp=at)
