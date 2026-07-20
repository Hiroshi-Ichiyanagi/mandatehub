"""
intent/lifecycle.py — 委任枠の状態を監査ログから再導出する（状態は保存しない）。

pause / resume / revoke / top-up はすべて監査イベントとして刻まれ、状態は
timestamp <= at のイベントを畳み込んで得る。generator / engine は不経済な状態
（担保不足・予算超過）で例外を投げず、真偽フラグで attest する。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from mandatehub.intent.settlement import KEY_MANDATE_ID

EVT_PAUSED = "mandate_paused"
EVT_RESUMED = "mandate_resumed"
EVT_REVOKED = "mandate_revoked"
EVT_TOPPED_UP = "mandate_topped_up"


class MandateState(enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class MandateLifecycleView:
    state: MandateState
    effective_budget_cap_cents: int  # base + Σ（timestamp <= at の top-up）
    paused: bool
    revoked: bool
    last_event_sequence: int | None


def fold_lifecycle(
    events: Iterable,
    *,
    mandate_id: str,
    base_budget_cap_cents: int,
    valid_until: datetime,
    at: datetime,
) -> MandateLifecycleView:
    """監査イベントを畳み込んで at 時点の委任枠状態を返す。"""
    paused = False
    revoked = False
    effective_cap = base_budget_cap_cents
    last_seq: int | None = None

    # AuditLog.append は呼び出し側の timestamp を強制しない（settlement と違い単調性
    # チェックが無い）。sequence 順に畳むと、バックデートした resume が後から pause を
    # 打ち消してしまう。時刻順（同時刻は sequence 順）に畳んで「時間的に最後」を勝たせる。
    relevant = sorted(
        (
            e
            for e in events
            if e.timestamp <= at and e.payload.get(KEY_MANDATE_ID) == mandate_id
        ),
        key=lambda e: (e.timestamp, e.sequence),
    )
    for ev in relevant:
        if ev.event_type == EVT_REVOKED:
            revoked = True
            last_seq = ev.sequence
        elif ev.event_type == EVT_PAUSED:
            if not revoked:
                paused = True
            last_seq = ev.sequence
        elif ev.event_type == EVT_RESUMED:
            if not revoked:
                paused = False
            last_seq = ev.sequence
        elif ev.event_type == EVT_TOPPED_UP:
            add = ev.payload.get("add_collateral_cents", 0)
            if isinstance(add, int):
                effective_cap += add
            last_seq = ev.sequence

    if revoked:
        state = MandateState.REVOKED
    elif at > valid_until:
        state = MandateState.EXPIRED
    elif paused:
        state = MandateState.PAUSED
    else:
        state = MandateState.ACTIVE

    return MandateLifecycleView(
        state=state,
        effective_budget_cap_cents=effective_cap,
        paused=paused,
        revoked=revoked,
        last_event_sequence=last_seq,
    )
