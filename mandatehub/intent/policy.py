"""
intent/policy.py — SpendPolicy / 決定論的 epoch / velocity（すべて純粋）。

epoch 境界は明示 anchor からの整数マイクロ秒で計算し、float を返す
timedelta.total_seconds() は決定論経路で一切使わない（テストで grep 禁止）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mandatehub.intent.errors import MandateError


@dataclass(frozen=True)
class EpochSpec:
    """固定長 epoch の仕様。anchor を epoch-0 の開始点とする。"""

    anchor: datetime
    length_seconds: int  # > 0

    def __post_init__(self) -> None:
        if not isinstance(self.length_seconds, int) or self.length_seconds <= 0:
            raise MandateError("EpochSpec.length_seconds must be a positive int")

    def _micros_since_anchor(self, at: datetime) -> int:
        d = at - self.anchor  # timedelta
        return d.days * 86_400_000_000 + d.seconds * 1_000_000 + d.microseconds  # 厳密整数

    def epoch_index(self, at: datetime) -> int:
        us = self._micros_since_anchor(at)
        if us < 0:
            return -1  # anchor 前バケット
        return us // (self.length_seconds * 1_000_000)

    def epoch_bounds(self, idx: int) -> tuple[datetime, datetime]:
        start = self.anchor + timedelta(seconds=idx * self.length_seconds)
        return start, start + timedelta(seconds=self.length_seconds)


@dataclass(frozen=True)
class SpendPolicy:
    """委任枠に付随する追加ルール（frozen / hashable / canonical）。"""

    payee_allowlist: frozenset[str] | None = None
    purpose_sub_budgets: tuple[tuple[str, int], ...] = ()  # 構築時に purpose 昇順に正規化
    min_amount_cents: int | None = None
    max_amount_cents: int | None = None
    epoch: EpochSpec | None = None
    epoch_spend_cap_cents: int | None = None
    epoch_settlement_cap: int | None = None  # velocity: epoch あたり最大 N 件
    rolling_window_seconds: int | None = None
    rolling_window_spend_cap_cents: int | None = None
    rolling_window_settlement_cap: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "min_amount_cents",
            "max_amount_cents",
            "epoch_spend_cap_cents",
            "epoch_settlement_cap",
            "rolling_window_seconds",
            "rolling_window_spend_cap_cents",
            "rolling_window_settlement_cap",
        ):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or v <= 0):
                raise MandateError(f"SpendPolicy.{name} must be a positive int if set, got {v!r}")
        if (
            self.min_amount_cents is not None
            and self.max_amount_cents is not None
            and self.min_amount_cents > self.max_amount_cents
        ):
            raise MandateError("SpendPolicy.min_amount_cents must be <= max_amount_cents")

        # purpose_sub_budgets: 正規化（purpose 昇順・一意・cap>0）
        seen: set[str] = set()
        norm: list[tuple[str, int]] = []
        for purpose, cap in self.purpose_sub_budgets:
            if purpose in seen:
                raise MandateError(f"duplicate purpose in sub-budget: {purpose}")
            if not isinstance(cap, int) or cap <= 0:
                raise MandateError(f"sub-budget cap must be positive int for {purpose}")
            seen.add(purpose)
            norm.append((purpose, cap))
        norm.sort(key=lambda x: x[0])
        object.__setattr__(self, "purpose_sub_budgets", tuple(norm))

        # epoch_* は epoch が必須、window_* は rolling_window_seconds が必須
        if (self.epoch_spend_cap_cents is not None or self.epoch_settlement_cap is not None) and self.epoch is None:
            raise MandateError("epoch caps require an EpochSpec")
        if (
            self.rolling_window_spend_cap_cents is not None
            or self.rolling_window_settlement_cap is not None
        ) and self.rolling_window_seconds is None:
            raise MandateError("window caps require rolling_window_seconds")

    def sub_budget_for(self, purpose: str) -> int | None:
        for p, cap in self.purpose_sub_budgets:
            if p == purpose:
                return cap
        return None
