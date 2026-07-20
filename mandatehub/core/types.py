"""
core/types.py — 金融プリミティブ型の定義。

すべての金額は整数（cents単位）で保持し、浮動小数点演算を一切使わない。
イミュータブルな値オブジェクトとして実装し、書き換えによるバグを構造的に防ぐ。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class Currency(enum.Enum):
    """対応通貨。法定通貨と電子決済手段（ステーブルコイン）を統一して扱う。

    minor_units は cents への変換係数。例えば JPY は最小単位が円（10^0）、
    USD は最小単位がセント（10^2）、USDC は最小単位が 10^6。
    """

    JPY = ("JPY", 0, "日本円")
    USD = ("USD", 2, "米ドル")
    EUR = ("EUR", 2, "ユーロ")
    GBP = ("GBP", 2, "英ポンド")

    # ステーブルコイン（電子決済手段）
    USDC = ("USDC", 6, "USD Coin")
    USDT = ("USDT", 6, "Tether USD")
    JPYC = ("JPYC", 18, "JPY Coin")
    PROGMAT_JPY = ("PROGMAT_JPY", 0, "Progmat JPY")

    def __init__(self, code: str, minor_units: int, display_name: str) -> None:
        self.code = code
        self.minor_units = minor_units
        self.display_name = display_name

    @property
    def smallest_unit_per_unit(self) -> int:
        """1単位あたりの最小単位数。例: USD なら 100, JPY なら 1。"""
        return 10**self.minor_units


@dataclass(frozen=True)
class Money:
    """金額を表すイミュータブルな値オブジェクト。

    内部表現は最小単位の整数（cents）。例えば USD 1.23 は cents=123。
    JPY 100 円は cents=100（minor_units=0なので）。
    浮動小数点は一切使わない。
    """

    cents: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int):
            raise TypeError(
                f"Money.cents must be int, got {type(self.cents).__name__}. "
                f"Floats are forbidden to prevent rounding errors."
            )
        if not isinstance(self.currency, Currency):
            raise TypeError(
                f"Money.currency must be Currency enum, got {type(self.currency).__name__}"
            )

    @classmethod
    def from_units(cls, units: int, currency: Currency) -> Money:
        """主単位（円・ドル・ユーロ）から Money を構築する。

        Money.from_units(100, Currency.USD) は 100ドル（cents=10000）を表す。
        """
        return cls(cents=units * currency.smallest_unit_per_unit, currency=currency)

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(cents=0, currency=currency)

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(cents=self.cents + other.cents, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(cents=self.cents - other.cents, currency=self.currency)

    def __neg__(self) -> Money:
        return Money(cents=-self.cents, currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents < other.cents

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents <= other.cents

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents > other.cents

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents >= other.cents

    def is_zero(self) -> bool:
        return self.cents == 0

    def is_positive(self) -> bool:
        return self.cents > 0

    def is_negative(self) -> bool:
        return self.cents < 0

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"Cannot operate on {self.currency.code} and {other.currency.code} directly. "
                f"Use FX conversion explicitly."
            )

    def __str__(self) -> str:
        if self.currency.minor_units == 0:
            return f"{self.cents:,} {self.currency.code}"
        # 負の値の場合に floor division が想定外の結果を返すため、絶対値で計算してから符号を付与する
        abs_cents = abs(self.cents)
        major = abs_cents // self.currency.smallest_unit_per_unit
        minor = abs_cents % self.currency.smallest_unit_per_unit
        sign = "-" if self.cents < 0 else ""
        return f"{sign}{major:,}.{minor:0{self.currency.minor_units}d} {self.currency.code}"


class CurrencyMismatchError(Exception):
    """異なる通貨間の演算が試みられたときに送出される。"""


class OwnerType(enum.Enum):
    """口座保有者の区分。

    USER: 利用者口座
    PLATFORM: プラットフォーム保有口座（顧客資金プール等）
    RESERVE: 準備資産口座（信託・供託・銀行預け）
    FEE: 手数料収入口座
    FX_GAIN_LOSS: 為替差損益口座（複式簿記の貸借合わせ）
    REGULATORY: 規制関連口座（供託、保証金など）
    CLEARING: クリアリング口座（決済中の中間勘定）
    """

    USER = "USER"
    PLATFORM = "PLATFORM"
    RESERVE = "RESERVE"
    FEE = "FEE"
    FX_GAIN_LOSS = "FX_GAIN_LOSS"
    REGULATORY = "REGULATORY"
    CLEARING = "CLEARING"


@dataclass(frozen=True)
class Account:
    """口座のメタデータ。残高は元帳の Entry から計算するため、ここには保持しない。"""

    account_id: str
    owner_type: OwnerType
    currency: Currency
    label: str
    regulatory_tags: frozenset[str] = field(default_factory=frozenset)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(
        cls,
        owner_type: OwnerType,
        currency: Currency,
        label: str,
        regulatory_tags: frozenset[str] | None = None,
    ) -> Account:
        return cls(
            account_id=str(uuid.uuid4()),
            owner_type=owner_type,
            currency=currency,
            label=label,
            regulatory_tags=regulatory_tags or frozenset(),
        )


@dataclass(frozen=True)
class Entry:
    """単一の貸借エントリー。トランザクション内で必ず複数生成される。

    amount は符号付き。借方（資産・費用の増加、負債・収益の減少）はプラス、
    貸方（その逆）はマイナス、という会計の伝統的な符号規約に従う。
    ただし本実装では「資金の流れ方向」をシンプルに扱うため、
    送金元口座でマイナス、送金先口座でプラスとする運用にする。
    """

    entry_id: str
    transaction_id: str
    account_id: str
    amount: Money
    sequence: int  # トランザクション内での順序（0始まり）

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Money):
            raise TypeError(f"Entry.amount must be Money, got {type(self.amount).__name__}")


class TransactionStatus(enum.Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    REVERSED = "REVERSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ComplianceDecision:
    """1トランザクションに対するコンプライアンス判定の記録。

    判定根拠を構造化して保持することで、規制当局・利用者・買い手企業の
    すべてに対する説明責任を果たす。
    """

    decision: str  # APPROVED, REJECTED, REVIEW_REQUIRED
    risk_score: int  # 0-100
    triggered_rules: tuple[str, ...]
    explanation: str
    rule_version: str
    decided_at: datetime
    decided_by: str  # AML_ENGINE_v1, MANUAL_REVIEW_user_xxx, etc.


@dataclass(frozen=True)
class Transaction:
    """1つの取引を表すイミュータブルなレコード。

    複式簿記の単位。複数の Entry を含み、通貨ごとに合計が0であることを保証する。
    """

    transaction_id: str
    entries: tuple[Entry, ...]
    purpose_code: str  # SALARY, GOODS_PAYMENT, GIFT, INVESTMENT, FX_CONVERT, etc.
    initiator_id: str
    initiated_at: datetime
    settled_at: datetime | None
    status: TransactionStatus
    external_refs: tuple[tuple[str, str], ...]  # ((provider, ref), ...)
    compliance_decision: ComplianceDecision | None
    metadata: tuple[tuple[str, str], ...]  # 任意のkey-valueメタデータ

    def __post_init__(self) -> None:
        # 複式簿記の検証：通貨ごとに合計が0でなければならない
        currency_sums: dict[Currency, int] = {}
        for entry in self.entries:
            currency_sums[entry.amount.currency] = (
                currency_sums.get(entry.amount.currency, 0) + entry.amount.cents
            )
        for currency, total in currency_sums.items():
            if total != 0:
                raise UnbalancedTransactionError(
                    f"Transaction {self.transaction_id} is unbalanced for {currency.code}: "
                    f"sum of entries = {total} cents (must be 0). "
                    f"Use FX_GAIN_LOSS account to absorb FX differences."
                )

        # entry_id とトランザクションIDの整合性
        for entry in self.entries:
            if entry.transaction_id != self.transaction_id:
                raise IntegrityError(
                    f"Entry {entry.entry_id} has transaction_id {entry.transaction_id}, "
                    f"but parent Transaction has {self.transaction_id}"
                )

    def affected_accounts(self) -> set[str]:
        return {e.account_id for e in self.entries}

    def total_amount_for_account(self, account_id: str) -> dict[Currency, int]:
        """指定口座について、通貨ごとの増減合計を返す。"""
        result: dict[Currency, int] = {}
        for e in self.entries:
            if e.account_id == account_id:
                result[e.amount.currency] = (
                    result.get(e.amount.currency, 0) + e.amount.cents
                )
        return result


class UnbalancedTransactionError(Exception):
    """トランザクションの貸借が一致しないときに送出される。"""


class IntegrityError(Exception):
    """データ整合性の違反が検出されたときに送出される。"""


def new_transaction_id() -> str:
    return f"tx_{uuid.uuid4().hex}"


def new_entry_id() -> str:
    return f"e_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class PendingAmlReview:
    """AML 判定が REVIEW_REQUIRED となり、人手解決を待っている収益トランザクション。

    アプリケーション層が人手レビュー一覧として返す閲覧用ビュー。
    """

    transaction_id: str
    creator_id: str
    gross: "Money"
    occurred_at: datetime
    risk_score: int
    aml_reasons: str  # AML エンジンが REVIEW_REQUIRED とした理由 (説明文)


@dataclass(frozen=True)
class AmlResolutionResult:
    """REVIEW_REQUIRED トランザクションの人手解決 (approve / reject) の結果。

    resolution 結果には creator_id を含め、解決以降のトレーサビリティを担保する。
    """

    transaction_id: str
    resolution: str  # "APPROVED" | "REJECTED"
    creator_id: str
    resolved_at: datetime
    reviewer_note: str
    fx_disclosure_ids: tuple[str, ...] = ()  # approve かつ即時 FX 変換時のみ非空
    audit_sequence: int | None = None  # audit ログに記録された解決イベントの sequence
