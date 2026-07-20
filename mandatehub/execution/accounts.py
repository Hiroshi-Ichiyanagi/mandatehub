"""
execution/accounts.py — 執行（best-execution / surplus 分配）に使う口座束の記述。

新しい OwnerType は追加しない。役割は既存の 7 種の OwnerType に
Account.label / regulatory_tags で束ねる（§ owner-type モデル）：

  payee            OwnerType.USER or PLATFORM   tag PAYEE
  user_rebate      OwnerType.USER               tag REBATE
  operator_margin  OwnerType.FEE                tag OPERATOR_MARGIN   （唯一の残差受領先）
  gas              OwnerType.FEE                tag GAS
  referrer         OwnerType.PLATFORM           tag REFERRER          （任意）
  venue_clearing   OwnerType.CLEARING           tag VENUE_CLEARING    （C_in != C_out のとき必須）

重要（USER バケット二重集計の回避）：rebate も payee も（自己保管型では escrow も）
USER になり得るため、OwnerType.USER に対する集計は必ず regulatory_tags で
絞り込む。証明側は aggregate_by(USER) を使わず、ここで名指しした口座 ID を使う。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionAccounts:
    """1 回の best-execution 決済で価値が着地する口座の束。

    各口座の通貨・owner_type の妥当性は決済時（bridge）に検証され、
    不整合は CURRENCY_MISMATCH 等で却下される。
    """

    payee_account_id: str
    user_rebate_account_id: str
    operator_margin_account_id: str
    gas_account_id: str
    referrer_account_id: str | None = None
    # Model B（C_in != C_out）で必須。元帳は 1 口座 = 1 通貨のため、venue ミラーは
    # 通貨ごとに別口座（in 側 = C_in、out 側 = C_out）で保持する。
    venue_clearing_account_id: str | None = None  # C_in 平面のミラー
    venue_clearing_out_account_id: str | None = None  # C_out 平面のミラー
