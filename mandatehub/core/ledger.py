"""
core/ledger.py — 複式簿記元帳エンジン。

Append-onlyジャーナルとして実装され、一度記録されたエントリーは絶対に削除・修正されない。
訂正は反対仕訳で表現する。残高は常にエントリー集合から計算される。

このエンジンの役目は3つ：
1. トランザクションの構造的妥当性（貸借一致など）を保証する。
2. 残高をいつでも、過去のいつの時点でも、正確に再計算できる。
3. 全イベントを Audit Log に流して透明性エンジンに接続する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterable

from mandatehub.core.storage import LedgerStorage
from mandatehub.core.types import (
    Account,
    ComplianceDecision,
    Currency,
    Entry,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
    UnbalancedTransactionError,
    new_entry_id,
    new_transaction_id,
)


class TransactionBuilder:
    """1つのトランザクションを段階的に構築するためのビルダー。

    複数のエントリーを追加し、最後に build() で確定する。
    build時に複式簿記の制約（通貨ごとの合計が0）を検証する。
    """

    def __init__(
        self,
        purpose_code: str,
        initiator_id: str,
        initiated_at: datetime | None = None,
    ) -> None:
        self._transaction_id = new_transaction_id()
        self._purpose_code = purpose_code
        self._initiator_id = initiator_id
        self._initiated_at = initiated_at or datetime.now(timezone.utc)
        self._entries: list[Entry] = []
        self._external_refs: list[tuple[str, str]] = []
        self._metadata: list[tuple[str, str]] = []
        self._compliance_decision: ComplianceDecision | None = None

    @property
    def transaction_id(self) -> str:
        return self._transaction_id

    def add_entry(self, account_id: str, amount: Money) -> TransactionBuilder:
        """口座に対する増減エントリーを追加する。amountは符号付き。"""
        entry = Entry(
            entry_id=new_entry_id(),
            transaction_id=self._transaction_id,
            account_id=account_id,
            amount=amount,
            sequence=len(self._entries),
        )
        self._entries.append(entry)
        return self

    def transfer(
        self,
        from_account_id: str,
        to_account_id: str,
        amount: Money,
    ) -> TransactionBuilder:
        """同一通貨内での口座間振替。シンプルな2エントリーを生成する。"""
        if not amount.is_positive():
            raise ValueError(f"Transfer amount must be positive, got {amount}")
        self.add_entry(from_account_id, -amount)
        self.add_entry(to_account_id, amount)
        return self

    def fx_convert(
        self,
        from_account_id: str,
        from_amount: Money,
        to_account_id: str,
        to_amount: Money,
        fx_gain_loss_account_id: str,
    ) -> TransactionBuilder:
        """為替変換。

        from_account から from_amount が減り、to_account に to_amount が増える。
        通貨が異なるため、各通貨ごとに貸借を合わせる必要がある。
        FX口座が両通貨でバッファとして機能する。

        実装上は：
          from通貨側: from_account -from_amount, fx_gain_loss +from_amount
          to通貨側:   fx_gain_loss -to_amount, to_account +to_amount

        この設計により、fx_gain_loss口座は両通貨の残高を保持し、
        その差額が為替差損益として可視化される。
        """
        if from_amount.currency == to_amount.currency:
            raise ValueError(
                f"fx_convert requires different currencies, got {from_amount.currency.code} -> {to_amount.currency.code}"
            )
        if not from_amount.is_positive() or not to_amount.is_positive():
            raise ValueError("FX amounts must be positive")

        self.add_entry(from_account_id, -from_amount)
        self.add_entry(fx_gain_loss_account_id, from_amount)
        self.add_entry(fx_gain_loss_account_id, -to_amount)
        self.add_entry(to_account_id, to_amount)
        return self

    def with_external_ref(self, provider: str, ref: str) -> TransactionBuilder:
        """外部プロバイダーの参照IDを付与する。"""
        self._external_refs.append((provider, ref))
        return self

    def with_metadata(self, key: str, value: str) -> TransactionBuilder:
        self._metadata.append((key, value))
        return self

    def with_compliance_decision(self, decision: ComplianceDecision) -> TransactionBuilder:
        self._compliance_decision = decision
        return self

    def build(
        self,
        status: TransactionStatus = TransactionStatus.PENDING,
        settled_at: datetime | None = None,
    ) -> Transaction:
        if not self._entries:
            raise ValueError("Transaction must have at least one entry")
        return Transaction(
            transaction_id=self._transaction_id,
            entries=tuple(self._entries),
            purpose_code=self._purpose_code,
            initiator_id=self._initiator_id,
            initiated_at=self._initiated_at,
            settled_at=settled_at,
            status=status,
            external_refs=tuple(self._external_refs),
            compliance_decision=self._compliance_decision,
            metadata=tuple(self._metadata),
        )


class Ledger:
    """複式簿記元帳。

    口座の登録、トランザクションの記録、残高照会の3つを提供する。
    全変更は LedgerStorage を経由して永続化される。

    Append-only原則：post() で記録されたトランザクションは削除・修正できない。
    訂正は reverse() で反対仕訳を生成する。
    """

    def __init__(self, storage: LedgerStorage) -> None:
        self._storage = storage
        self._listeners: list[Callable[[str, dict], None]] = []

    # ---------- 口座管理 ----------

    def open_account(
        self,
        owner_type: OwnerType,
        currency: Currency,
        label: str,
        regulatory_tags: frozenset[str] | None = None,
    ) -> Account:
        account = Account.new(
            owner_type=owner_type,
            currency=currency,
            label=label,
            regulatory_tags=regulatory_tags,
        )
        self._storage.save_account(account)
        self._emit("account_opened", {"account_id": account.account_id, "label": account.label})
        return account

    def get_account(self, account_id: str) -> Account:
        return self._storage.load_account(account_id)

    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]:
        return self._storage.list_accounts(owner_type=owner_type)

    # ---------- トランザクション ----------

    def post(self, transaction: Transaction) -> Transaction:
        """トランザクションを元帳に記録する。

        この時点で複式簿記の制約は Transaction.__post_init__ で検証済み。
        ここでは追加で口座の存在確認と通貨整合性を確認する。
        """
        # 口座の存在確認と通貨整合性
        for entry in transaction.entries:
            account = self._storage.load_account(entry.account_id)
            if account.currency != entry.amount.currency:
                raise UnbalancedTransactionError(
                    f"Entry currency {entry.amount.currency.code} does not match "
                    f"account currency {account.currency.code} for account {account.account_id}"
                )

        self._storage.save_transaction(transaction)
        self._emit(
            "transaction_posted",
            {
                "transaction_id": transaction.transaction_id,
                "purpose_code": transaction.purpose_code,
                "status": transaction.status.value,
                "num_entries": len(transaction.entries),
                "affected_accounts": sorted(transaction.affected_accounts()),
            },
        )
        return transaction

    def settle(self, transaction_id: str, settled_at: datetime | None = None) -> Transaction:
        """PENDING状態のトランザクションをSETTLEDに遷移させる。"""
        tx = self._storage.load_transaction(transaction_id)
        if tx.status != TransactionStatus.PENDING:
            raise ValueError(
                f"Cannot settle transaction {transaction_id} in status {tx.status.value}"
            )
        new_tx = replace(
            tx,
            status=TransactionStatus.SETTLED,
            settled_at=settled_at or datetime.now(timezone.utc),
        )
        self._storage.update_transaction_status(new_tx)
        self._emit(
            "transaction_settled",
            {"transaction_id": transaction_id, "settled_at": new_tx.settled_at.isoformat()},
        )
        return new_tx

    def fail_transaction(self, transaction_id: str, reason: str) -> Transaction:
        """PENDING 状態のトランザクションを FAILED に遷移させる。

        AML レビューで却下された収益トランザクションなど、SETTLED に至らず終了する
        取引に使う。PENDING のエントリは残高 (SETTLED 集計) に算入されないため、
        反対仕訳は不要 — 状態遷移のみで「この取引は成立しなかった」を表現する。
        """
        tx = self._storage.load_transaction(transaction_id)
        if tx.status != TransactionStatus.PENDING:
            raise ValueError(
                f"Cannot fail transaction {transaction_id} in status {tx.status.value}"
            )
        new_tx = replace(tx, status=TransactionStatus.FAILED)
        self._storage.update_transaction_status(new_tx)
        self._emit(
            "transaction_failed",
            {"transaction_id": transaction_id, "reason": reason},
        )
        return new_tx

    def reverse(
        self,
        transaction_id: str,
        reason: str,
        initiator_id: str,
    ) -> Transaction:
        """既存トランザクションの反対仕訳を生成し、新トランザクションとして記録する。

        元のトランザクションは変更しない。Append-only原則の徹底。
        """
        original = self._storage.load_transaction(transaction_id)
        builder = TransactionBuilder(
            purpose_code=f"REVERSAL_OF:{original.purpose_code}",
            initiator_id=initiator_id,
        )
        for entry in original.entries:
            builder.add_entry(entry.account_id, -entry.amount)
        builder.with_metadata("reversal_of", transaction_id)
        builder.with_metadata("reversal_reason", reason)

        reversal_tx = builder.build(status=TransactionStatus.SETTLED, settled_at=datetime.now(timezone.utc))
        self._storage.save_transaction(reversal_tx)
        self._emit(
            "transaction_reversed",
            {
                "original_transaction_id": transaction_id,
                "reversal_transaction_id": reversal_tx.transaction_id,
                "reason": reason,
            },
        )
        return reversal_tx

    def get_transaction(self, transaction_id: str) -> Transaction:
        return self._storage.load_transaction(transaction_id)

    # ---------- 残高照会 ----------

    def balance(
        self,
        account_id: str,
        as_of: datetime | None = None,
        include_pending: bool = False,
    ) -> Money:
        """口座の残高を、指定時点の SETTLED 済みトランザクションから計算する。

        as_of=None の場合は現時点。include_pending=True の場合は PENDING も含める。
        """
        account = self._storage.load_account(account_id)
        as_of = as_of or datetime.now(timezone.utc)

        valid_statuses = (
            (TransactionStatus.SETTLED, TransactionStatus.PENDING)
            if include_pending
            else (TransactionStatus.SETTLED,)
        )

        total_cents = 0
        for entry in self._storage.iter_entries_for_account(
            account_id=account_id, as_of=as_of, statuses=valid_statuses
        ):
            total_cents += entry.amount.cents

        return Money(cents=total_cents, currency=account.currency)

    def all_balances_snapshot(
        self,
        as_of: datetime | None = None,
        include_pending: bool = False,
    ) -> dict[str, Money]:
        """全口座の残高を一括で取得する。Proof of Reservesなどの基礎データ。"""
        as_of = as_of or datetime.now(timezone.utc)
        result: dict[str, Money] = {}
        for account in self._storage.list_accounts():
            result[account.account_id] = self.balance(
                account.account_id, as_of=as_of, include_pending=include_pending
            )
        return result

    def aggregate_by(
        self,
        owner_type: OwnerType,
        currency: Currency,
        as_of: datetime | None = None,
    ) -> Money:
        """指定 owner_type / currency の総和。利用者負債合計などに使う。"""
        as_of = as_of or datetime.now(timezone.utc)
        total = Money.zero(currency)
        for account in self._storage.list_accounts(owner_type=owner_type):
            if account.currency != currency:
                continue
            total = total + self.balance(account.account_id, as_of=as_of)
        return total

    # ---------- イベントリスナ ----------

    def add_listener(self, listener: Callable[[str, dict], None]) -> None:
        """元帳イベントを監視するリスナを登録する。透明性エンジンが接続する。"""
        self._listeners.append(listener)

    def _emit(self, event_type: str, payload: dict) -> None:
        for listener in self._listeners:
            try:
                listener(event_type, payload)
            except Exception:
                # リスナーの例外で元帳が壊れないようにする
                # 本番では監視ログに出す
                pass

    # ---------- ユーティリティ ----------

    def iter_all_transactions(self) -> Iterable[Transaction]:
        """全トランザクションを時系列で返す。監査・検証用。"""
        return self._storage.iter_all_transactions()
