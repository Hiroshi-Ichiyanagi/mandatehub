"""
core/storage.py — SQLiteベースの永続化層。

ACIDトランザクション、WALモード、append-only制約を実装する。
SQLiteはM2 Pro 16GBで十分なパフォーマンスを発揮し、
シングルファイルでの可搬性が高いため、検証段階に最適。

本番でPostgreSQLに移行する際は、このモジュールを差し替えるだけで済むよう、
LedgerStorageインターフェースを抽象化しておく。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Iterator, Protocol

from mandatehub.core.types import (
    Account,
    ComplianceDecision,
    Currency,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
)


# --------- 抽象インターフェース ---------


class LedgerStorage(Protocol):
    def save_account(self, account: Account) -> None: ...
    def load_account(self, account_id: str) -> Account: ...
    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]: ...
    def save_transaction(self, transaction: Transaction) -> None: ...
    def load_transaction(self, transaction_id: str) -> Transaction: ...
    def update_transaction_status(self, transaction: Transaction) -> None: ...
    def iter_entries_for_account(
        self,
        account_id: str,
        as_of: datetime,
        statuses: tuple[TransactionStatus, ...],
    ) -> Iterator[Entry]: ...
    def iter_all_transactions(self) -> Iterator[Transaction]: ...

    def try_claim(self, key: str) -> bool:
        """委任枠決済のリプレイ一意性を storage 層で原子的に主張する。

        初めて見る key なら True（＝この呼び出しが唯一の勝者）、既に存在すれば False。
        DB の一意制約で実装するため、複数ワーカー/プロセスでも二重決済が構造的に不可能
        （read-then-check の競合窓を閉じる）。省略可（未実装なら Ledger 側が単一プロセス
        向けに True を返す）。
        """
        ...


# --------- SQLite実装 ---------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    label TEXT NOT NULL,
    regulatory_tags TEXT NOT NULL,  -- JSON array
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_owner_type ON accounts(owner_type);
CREATE INDEX IF NOT EXISTS idx_accounts_currency ON accounts(currency);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    purpose_code TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    initiated_at TEXT NOT NULL,
    settled_at TEXT,
    status TEXT NOT NULL,
    external_refs TEXT NOT NULL,    -- JSON array of [provider, ref] pairs
    metadata TEXT NOT NULL,         -- JSON array of [key, value] pairs
    compliance_decision TEXT        -- JSON object or NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_initiated_at ON transactions(initiated_at);
CREATE INDEX IF NOT EXISTS idx_transactions_settled_at ON transactions(settled_at);

CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_account_id ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_transaction_id ON entries(transaction_id);

CREATE TABLE IF NOT EXISTS settlement_claims (
    claim_key  TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL
);
"""


class SQLiteLedgerStorage:
    """SQLiteベースの元帳ストレージ。

    Append-only制約はアプリケーション層で強制する（save_transactionは既存IDで失敗する）。
    トランザクションのステータス更新のみ update_transaction_status() で許可するが、
    エントリーは絶対に変更しない。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------- 口座 ----------

    def save_account(self, account: Account) -> None:
        try:
            self._conn.execute(
                "INSERT INTO accounts (account_id, owner_type, currency, label, regulatory_tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    account.account_id,
                    account.owner_type.value,
                    account.currency.code,
                    account.label,
                    json.dumps(sorted(account.regulatory_tags)),
                    account.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise IntegrityError(f"Account already exists: {account.account_id}") from e

    def load_account(self, account_id: str) -> Account:
        row = self._conn.execute(
            "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
            "FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"Account not found: {account_id}")
        return Account(
            account_id=row[0],
            owner_type=OwnerType(row[1]),
            currency=Currency[row[2]] if not row[2].startswith("PROGMAT") else Currency.PROGMAT_JPY,
            label=row[3],
            regulatory_tags=frozenset(json.loads(row[4])),
            created_at=datetime.fromisoformat(row[5]),
        )

    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]:
        if owner_type is None:
            rows = self._conn.execute(
                "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
                "FROM accounts ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
                "FROM accounts WHERE owner_type = ? ORDER BY created_at",
                (owner_type.value,),
            ).fetchall()
        return [
            Account(
                account_id=r[0],
                owner_type=OwnerType(r[1]),
                currency=_currency_from_code(r[2]),
                label=r[3],
                regulatory_tags=frozenset(json.loads(r[4])),
                created_at=datetime.fromisoformat(r[5]),
            )
            for r in rows
        ]

    # ---------- トランザクション ----------

    def save_transaction(self, transaction: Transaction) -> None:
        # アトミックに transactions と entries を書く
        try:
            self._conn.execute("BEGIN IMMEDIATE")

            existing = self._conn.execute(
                "SELECT 1 FROM transactions WHERE transaction_id = ?",
                (transaction.transaction_id,),
            ).fetchone()
            if existing:
                raise IntegrityError(
                    f"Transaction already exists: {transaction.transaction_id}. "
                    f"Append-only ledger: use reverse() to negate."
                )

            self._conn.execute(
                "INSERT INTO transactions "
                "(transaction_id, purpose_code, initiator_id, initiated_at, settled_at, "
                " status, external_refs, metadata, compliance_decision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transaction.transaction_id,
                    transaction.purpose_code,
                    transaction.initiator_id,
                    transaction.initiated_at.isoformat(),
                    transaction.settled_at.isoformat() if transaction.settled_at else None,
                    transaction.status.value,
                    json.dumps([list(r) for r in transaction.external_refs]),
                    json.dumps([list(m) for m in transaction.metadata]),
                    _serialize_compliance(transaction.compliance_decision),
                ),
            )

            for entry in transaction.entries:
                self._conn.execute(
                    "INSERT INTO entries "
                    "(entry_id, transaction_id, account_id, amount_cents, currency, sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entry.entry_id,
                        entry.transaction_id,
                        entry.account_id,
                        entry.amount.cents,
                        entry.amount.currency.code,
                        entry.sequence,
                    ),
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load_transaction(self, transaction_id: str) -> Transaction:
        row = self._conn.execute(
            "SELECT transaction_id, purpose_code, initiator_id, initiated_at, settled_at, "
            "       status, external_refs, metadata, compliance_decision "
            "FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"Transaction not found: {transaction_id}")

        entry_rows = self._conn.execute(
            "SELECT entry_id, transaction_id, account_id, amount_cents, currency, sequence "
            "FROM entries WHERE transaction_id = ? ORDER BY sequence",
            (transaction_id,),
        ).fetchall()

        entries = tuple(
            Entry(
                entry_id=er[0],
                transaction_id=er[1],
                account_id=er[2],
                amount=Money(cents=er[3], currency=_currency_from_code(er[4])),
                sequence=er[5],
            )
            for er in entry_rows
        )

        return Transaction(
            transaction_id=row[0],
            entries=entries,
            purpose_code=row[1],
            initiator_id=row[2],
            initiated_at=datetime.fromisoformat(row[3]),
            settled_at=datetime.fromisoformat(row[4]) if row[4] else None,
            status=TransactionStatus(row[5]),
            external_refs=tuple(tuple(r) for r in json.loads(row[6])),
            metadata=tuple(tuple(m) for m in json.loads(row[7])),
            compliance_decision=_deserialize_compliance(row[8]),
        )

    def update_transaction_status(self, transaction: Transaction) -> None:
        """ステータスとsettled_atのみ更新可能。エントリーは不変。"""
        self._conn.execute(
            "UPDATE transactions SET status = ?, settled_at = ? WHERE transaction_id = ?",
            (
                transaction.status.value,
                transaction.settled_at.isoformat() if transaction.settled_at else None,
                transaction.transaction_id,
            ),
        )

    # ---------- イテレーション ----------

    def iter_entries_for_account(
        self,
        account_id: str,
        as_of: datetime,
        statuses: tuple[TransactionStatus, ...],
    ) -> Iterator[Entry]:
        """指定口座について、as_of時点までの該当ステータスのエントリーを返す。

        as_of の判定は、SETTLED は settled_at <= as_of、PENDING は initiated_at <= as_of で行う。
        """
        status_values = [s.value for s in statuses]
        placeholders = ",".join("?" * len(status_values))
        query = f"""
            SELECT e.entry_id, e.transaction_id, e.account_id, e.amount_cents, e.currency, e.sequence
            FROM entries e
            JOIN transactions t ON e.transaction_id = t.transaction_id
            WHERE e.account_id = ?
              AND t.status IN ({placeholders})
              AND (
                  (t.status = 'SETTLED' AND t.settled_at <= ?)
                  OR (t.status = 'PENDING' AND t.initiated_at <= ?)
                  OR (t.status = 'REVERSED')
              )
            ORDER BY t.initiated_at, e.sequence
        """
        params = [account_id] + status_values + [as_of.isoformat(), as_of.isoformat()]
        for row in self._conn.execute(query, params):
            yield Entry(
                entry_id=row[0],
                transaction_id=row[1],
                account_id=row[2],
                amount=Money(cents=row[3], currency=_currency_from_code(row[4])),
                sequence=row[5],
            )

    def iter_all_transactions(self) -> Iterator[Transaction]:
        rows = self._conn.execute(
            "SELECT transaction_id FROM transactions ORDER BY initiated_at"
        ).fetchall()
        for row in rows:
            yield self.load_transaction(row[0])

    # ---------- リプレイ一意性（原子的 claim） ----------

    def try_claim(self, key: str) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO settlement_claims (claim_key, claimed_at) VALUES (?, ?)",
                (key, datetime.now(timezone.utc).isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


# --------- ヘルパー関数 ---------


def _currency_from_code(code: str) -> Currency:
    """通貨コード文字列から Currency enum を取得する。"""
    for c in Currency:
        if c.code == code:
            return c
    raise ValueError(f"Unknown currency code: {code}")


def _serialize_compliance(decision: ComplianceDecision | None) -> str | None:
    if decision is None:
        return None
    return json.dumps({
        "decision": decision.decision,
        "risk_score": decision.risk_score,
        "triggered_rules": list(decision.triggered_rules),
        "explanation": decision.explanation,
        "rule_version": decision.rule_version,
        "decided_at": decision.decided_at.isoformat(),
        "decided_by": decision.decided_by,
    })


def _deserialize_compliance(serialized: str | None) -> ComplianceDecision | None:
    if not serialized:
        return None
    data = json.loads(serialized)
    return ComplianceDecision(
        decision=data["decision"],
        risk_score=data["risk_score"],
        triggered_rules=tuple(data["triggered_rules"]),
        explanation=data["explanation"],
        rule_version=data["rule_version"],
        decided_at=datetime.fromisoformat(data["decided_at"]),
        decided_by=data["decided_by"],
    )
