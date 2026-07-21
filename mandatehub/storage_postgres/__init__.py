"""
storage_postgres — a shared-store LedgerStorage over PostgreSQL (optional `[postgres]` extra).

Mirrors SQLiteLedgerStorage exactly (same TEXT/ISO serialization, same append-only discipline)
so the ledger can be shared by multiple operator workers. The load-bearing addition is
`try_claim`, an atomic unique-PK claim (`INSERT … ON CONFLICT DO NOTHING`) that makes a
concurrent replay of the same intent structurally un-postable across workers/processes — the
storage-layer line of defense from docs/MULTIWORKER.md.

Core never imports this; `pip install 'mandatehub[postgres]'` (psycopg) enables it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone

from mandatehub.core.storage import (
    _currency_from_code,
    _deserialize_compliance,
    _serialize_compliance,
)
from mandatehub.core.types import (
    Account,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
)

try:
    import psycopg
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "PostgresLedgerStorage needs psycopg. Run: pip install 'mandatehub[postgres]'"
    ) from e


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    label TEXT NOT NULL,
    regulatory_tags TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_owner_type ON accounts(owner_type);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    purpose_code TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    initiated_at TEXT NOT NULL,
    settled_at TEXT,
    status TEXT NOT NULL,
    external_refs TEXT NOT NULL,
    metadata TEXT NOT NULL,
    compliance_decision TEXT
);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_initiated_at ON transactions(initiated_at);

CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    amount_cents BIGINT NOT NULL,
    currency TEXT NOT NULL,
    sequence INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_account_id ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_transaction_id ON entries(transaction_id);

CREATE TABLE IF NOT EXISTS settlement_claims (
    claim_key  TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL
);
"""


class PostgresLedgerStorage:
    """A LedgerStorage backed by PostgreSQL (multi-worker capable)."""

    def __init__(self, conninfo: str, *, autocommit: bool = True) -> None:
        self._conn = psycopg.connect(conninfo, autocommit=autocommit)
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------- accounts ----------

    def save_account(self, account: Account) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO accounts (account_id, owner_type, currency, label, "
                    "regulatory_tags, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (account.account_id, account.owner_type.value, account.currency.code,
                     account.label, json.dumps(sorted(account.regulatory_tags)),
                     account.created_at.isoformat()),
                )
        except psycopg.errors.UniqueViolation as e:
            raise IntegrityError(f"Account already exists: {account.account_id}") from e

    def load_account(self, account_id: str) -> Account:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
                "FROM accounts WHERE account_id = %s", (account_id,))
            row = cur.fetchone()
        if row is None:
            raise IntegrityError(f"Account not found: {account_id}")
        return _account(row)

    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]:
        with self._conn.cursor() as cur:
            if owner_type is None:
                cur.execute("SELECT account_id, owner_type, currency, label, regulatory_tags, "
                            "created_at FROM accounts ORDER BY created_at")
            else:
                cur.execute("SELECT account_id, owner_type, currency, label, regulatory_tags, "
                            "created_at FROM accounts WHERE owner_type = %s ORDER BY created_at",
                            (owner_type.value,))
            return [_account(r) for r in cur.fetchall()]

    # ---------- transactions ----------

    def save_transaction(self, transaction: Transaction) -> None:
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM transactions WHERE transaction_id = %s",
                            (transaction.transaction_id,))
                if cur.fetchone():
                    raise IntegrityError(
                        f"Transaction already exists: {transaction.transaction_id}. "
                        f"Append-only ledger: use reverse() to negate.")
                cur.execute(
                    "INSERT INTO transactions (transaction_id, purpose_code, initiator_id, "
                    "initiated_at, settled_at, status, external_refs, metadata, "
                    "compliance_decision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (transaction.transaction_id, transaction.purpose_code,
                     transaction.initiator_id, transaction.initiated_at.isoformat(),
                     transaction.settled_at.isoformat() if transaction.settled_at else None,
                     transaction.status.value,
                     json.dumps([list(r) for r in transaction.external_refs]),
                     json.dumps([list(m) for m in transaction.metadata]),
                     _serialize_compliance(transaction.compliance_decision)),
                )
                for entry in transaction.entries:
                    cur.execute(
                        "INSERT INTO entries (entry_id, transaction_id, account_id, "
                        "amount_cents, currency, sequence) VALUES (%s,%s,%s,%s,%s,%s)",
                        (entry.entry_id, entry.transaction_id, entry.account_id,
                         entry.amount.cents, entry.amount.currency.code, entry.sequence),
                    )
        except psycopg.errors.UniqueViolation as e:
            raise IntegrityError(
                f"Transaction already exists: {transaction.transaction_id}.") from e

    def load_transaction(self, transaction_id: str) -> Transaction:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT transaction_id, purpose_code, initiator_id, initiated_at, settled_at, "
                "status, external_refs, metadata, compliance_decision "
                "FROM transactions WHERE transaction_id = %s", (transaction_id,))
            row = cur.fetchone()
            if row is None:
                raise IntegrityError(f"Transaction not found: {transaction_id}")
            cur.execute(
                "SELECT entry_id, transaction_id, account_id, amount_cents, currency, sequence "
                "FROM entries WHERE transaction_id = %s ORDER BY sequence", (transaction_id,))
            entry_rows = cur.fetchall()
        entries = tuple(
            Entry(entry_id=er[0], transaction_id=er[1], account_id=er[2],
                  amount=Money(cents=er[3], currency=_currency_from_code(er[4])), sequence=er[5])
            for er in entry_rows)
        return Transaction(
            transaction_id=row[0], entries=entries, purpose_code=row[1], initiator_id=row[2],
            initiated_at=datetime.fromisoformat(row[3]),
            settled_at=datetime.fromisoformat(row[4]) if row[4] else None,
            status=TransactionStatus(row[5]),
            external_refs=tuple(tuple(r) for r in json.loads(row[6])),
            metadata=tuple(tuple(m) for m in json.loads(row[7])),
            compliance_decision=_deserialize_compliance(row[8]))

    def update_transaction_status(self, transaction: Transaction) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET status = %s, settled_at = %s WHERE transaction_id = %s",
                (transaction.status.value,
                 transaction.settled_at.isoformat() if transaction.settled_at else None,
                 transaction.transaction_id))

    # ---------- iteration ----------

    def iter_entries_for_account(self, account_id, as_of, statuses) -> Iterator[Entry]:
        status_values = [s.value for s in statuses]
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT e.entry_id, e.transaction_id, e.account_id, e.amount_cents, e.currency, "
                "e.sequence FROM entries e JOIN transactions t "
                "ON e.transaction_id = t.transaction_id "
                "WHERE e.account_id = %s AND t.status = ANY(%s) AND ("
                "  (t.status = 'SETTLED' AND t.settled_at <= %s)"
                "  OR (t.status = 'PENDING' AND t.initiated_at <= %s)"
                "  OR (t.status = 'REVERSED')) "
                "ORDER BY t.initiated_at, e.sequence",
                (account_id, status_values, as_of.isoformat(), as_of.isoformat()))
            for row in cur.fetchall():
                yield Entry(entry_id=row[0], transaction_id=row[1], account_id=row[2],
                            amount=Money(cents=row[3], currency=_currency_from_code(row[4])),
                            sequence=row[5])

    def iter_all_transactions(self) -> Iterator[Transaction]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT transaction_id FROM transactions ORDER BY initiated_at")
            ids = [r[0] for r in cur.fetchall()]
        for tid in ids:
            yield self.load_transaction(tid)

    # ---------- replay uniqueness (atomic claim) ----------

    def try_claim(self, key: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settlement_claims (claim_key, claimed_at) VALUES (%s, %s) "
                "ON CONFLICT (claim_key) DO NOTHING",
                (key, datetime.now(timezone.utc).isoformat()))
            return cur.rowcount == 1  # 1 = we inserted (won the claim); 0 = already claimed


def _account(row) -> Account:
    return Account(
        account_id=row[0], owner_type=OwnerType(row[1]),
        currency=_currency_from_code(row[2]), label=row[3],
        regulatory_tags=frozenset(json.loads(row[4])),
        created_at=datetime.fromisoformat(row[5]))


__all__ = ["PostgresLedgerStorage"]
