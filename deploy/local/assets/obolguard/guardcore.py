"""guardcore — obol-guard の純粋コア（policy + engine）を mandatehub 商品用に vendor した単一モジュール。

出自: obolguardmandatehub.zip (obol_guard 0.1.0, 2026-07-23) の policy.py + engine.py を
結合し、フラット import 化したもの。ステートフル部（state/approval/notify）は
ステートレスな guard-verify 商品には不要なので同梱しない。ロジックは無改変。

純粋・決定論的・fail-closed：I/O なし・時計なし・乱数なし（stdlib dataclasses/enum のみ）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

class PolicyError(ValueError):
    """ポリシー定義が不正なときに送出される。"""


def _check_pos_int(name: str, v: int | None) -> None:
    if v is None:
        return
    # bool は int のサブクラスなので明示的に弾く
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise PolicyError(f"{name} must be a positive int if set, got {v!r}")


@dataclass(frozen=True)
class GuardPolicy:
    """1 委任枠（mandate）に紐づく護衛ルール。

    金額はすべて最小単位の整数（USDC は 6 decimals を cents 相当の整数で扱う想定でも、
    呼び出し側が一貫して同じ単位を使えばよい）。None は「そのルール無効」。
    """

    # ---- ハード制約（違反 = DENY, fail-closed）----
    per_tx_max_cents: int | None = None            # 1 決済あたり上限
    min_amount_cents: int | None = None            # 1 決済あたり下限（埃レベルの弾き）
    window_seconds: int | None = None              # ローリング窓の長さ（velocity/spend の基準）
    window_spend_cap_cents: int | None = None      # 窓内の合計支出上限
    window_count_cap: int | None = None            # 窓内の最大決済件数
    daily_cap_cents: int | None = None             # 直近 24h の合計支出上限（ハード）
    payee_allowlist: frozenset[str] | None = None  # 設定時、ここに無い宛先は DENY
    payee_denylist: frozenset[str] = frozenset()   # ここにある宛先は常に DENY

    # ---- ソフトしきい値（超過 = REVIEW, 人手承認を要求）----
    per_tx_review_cents: int | None = None         # この額を超える 1 決済は人手承認
    daily_review_cents: int | None = None          # 直近 24h 累計がこの額を超えたら人手承認
    window_review_count: int | None = None         # 窓内件数がこれを超えたら人手承認（連打検知）
    review_new_payee: bool = False                 # 初めての宛先は人手承認

    def __post_init__(self) -> None:
        for name in (
            "per_tx_max_cents", "min_amount_cents", "window_seconds",
            "window_spend_cap_cents", "window_count_cap", "daily_cap_cents",
            "per_tx_review_cents", "daily_review_cents", "window_review_count",
        ):
            _check_pos_int(name, getattr(self, name))

        if (
            self.per_tx_max_cents is not None
            and self.min_amount_cents is not None
            and self.min_amount_cents > self.per_tx_max_cents
        ):
            raise PolicyError("min_amount_cents must be <= per_tx_max_cents")

        # 窓系ルールは window_seconds を必須にする（意味の通らない設定を早期に弾く）
        if (
            self.window_spend_cap_cents is not None
            or self.window_count_cap is not None
            or self.window_review_count is not None
        ) and self.window_seconds is None:
            raise PolicyError("window_* caps require window_seconds")

        if not isinstance(self.payee_denylist, frozenset):
            raise PolicyError("payee_denylist must be a frozenset")
        if self.payee_allowlist is not None and not isinstance(self.payee_allowlist, frozenset):
            raise PolicyError("payee_allowlist must be a frozenset or None")

        # allowlist と denylist の交差は設定ミス（許可かつ拒否）
        if self.payee_allowlist is not None:
            overlap = self.payee_allowlist & self.payee_denylist
            if overlap:
                raise PolicyError(f"payee in both allow and deny lists: {sorted(overlap)}")

    def has_soft_rules(self) -> bool:
        return any((
            self.per_tx_review_cents is not None,
            self.daily_review_cents is not None,
            self.window_review_count is not None,
            self.review_new_payee,
        ))


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"     # ハード違反：拒否（オンチェーン発注しない）
    REVIEW = "REVIEW"  # ソフト超過：人手承認（HITL）を待つ


@dataclass(frozen=True)
class Candidate:
    """評価対象の 1 決済。金額は最小単位の整数。at_ms は unix epoch ミリ秒。"""

    mandate_id: str
    payer: str
    payee: str
    amount_cents: int
    at_ms: int
    nonce: str = ""
    purpose: str = ""
    currency: str = "USDC"

    def __post_init__(self) -> None:
        if self.amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        if not self.mandate_id or not self.payee:
            raise ValueError("mandate_id and payee are required")


@dataclass(frozen=True)
class StateSnapshot:
    """評価に必要な積算値。呼び出し側（state.py 等）が候補時刻を基準に算出して渡す。"""

    spent_window_cents: int = 0   # 窓内の既確定支出（この候補を含まない）
    count_window: int = 0         # 窓内の既確定件数（この候補を含まない）
    spent_daily_cents: int = 0    # 直近 24h の既確定支出（この候補を含まない）
    seen_payee: bool = False      # この (mandate, payee) で過去に確定済みがあるか
    nonce_used: bool = False      # この nonce が既に消費済みか


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str                       # 先頭の決定的理由コード（ALLOW は "OK"）
    triggered: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    @property
    def needs_review(self) -> bool:
        return self.verdict is Verdict.REVIEW


def evaluate(policy: GuardPolicy, cand: Candidate, state: StateSnapshot) -> Decision:
    """純関数。副作用なし。fail-closed（判定不能な入力は上位で弾く前提）。"""

    # ---- ハード制約（違反 = DENY）。正準順序で最初の違反を返す ----
    if state.nonce_used:
        return Decision(Verdict.DENY, "NONCE_REUSED", ("NONCE_REUSED",))

    if cand.payee in policy.payee_denylist:
        return Decision(Verdict.DENY, "PAYEE_DENIED", ("PAYEE_DENIED",))

    if policy.payee_allowlist is not None and cand.payee not in policy.payee_allowlist:
        return Decision(Verdict.DENY, "PAYEE_NOT_ALLOWED", ("PAYEE_NOT_ALLOWED",))

    if policy.min_amount_cents is not None and cand.amount_cents < policy.min_amount_cents:
        return Decision(Verdict.DENY, "BELOW_MIN_AMOUNT", ("BELOW_MIN_AMOUNT",))

    if policy.per_tx_max_cents is not None and cand.amount_cents > policy.per_tx_max_cents:
        return Decision(Verdict.DENY, "PER_TX_LIMIT_EXCEEDED", ("PER_TX_LIMIT_EXCEEDED",))

    if policy.window_count_cap is not None and state.count_window + 1 > policy.window_count_cap:
        return Decision(Verdict.DENY, "WINDOW_COUNT_EXCEEDED", ("WINDOW_COUNT_EXCEEDED",))

    if (
        policy.window_spend_cap_cents is not None
        and state.spent_window_cents + cand.amount_cents > policy.window_spend_cap_cents
    ):
        return Decision(Verdict.DENY, "WINDOW_SPEND_EXCEEDED", ("WINDOW_SPEND_EXCEEDED",))

    if (
        policy.daily_cap_cents is not None
        and state.spent_daily_cents + cand.amount_cents > policy.daily_cap_cents
    ):
        return Decision(Verdict.DENY, "DAILY_CAP_EXCEEDED", ("DAILY_CAP_EXCEEDED",))

    # ---- ソフトしきい値（超過 = REVIEW）。該当を全て集めて返す ----
    triggered: list[str] = []

    if policy.review_new_payee and not state.seen_payee:
        triggered.append("NEW_PAYEE_REVIEW")

    if policy.per_tx_review_cents is not None and cand.amount_cents > policy.per_tx_review_cents:
        triggered.append("PER_TX_REVIEW")

    if (
        policy.daily_review_cents is not None
        and state.spent_daily_cents + cand.amount_cents > policy.daily_review_cents
    ):
        triggered.append("DAILY_REVIEW")

    if (
        policy.window_review_count is not None
        and state.count_window + 1 > policy.window_review_count
    ):
        triggered.append("VELOCITY_REVIEW")

    if triggered:
        return Decision(Verdict.REVIEW, triggered[0], tuple(triggered))

    return Decision(Verdict.ALLOW, "OK", ())
