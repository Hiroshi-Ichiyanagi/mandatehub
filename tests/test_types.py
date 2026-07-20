"""core/types.py に対するテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.types import (
    Account,
    Currency,
    CurrencyMismatchError,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
    UnbalancedTransactionError,
    new_entry_id,
    new_transaction_id,
)


# ---------- Money ----------


class TestMoney:
    def test_construct_with_cents(self):
        m = Money(cents=12345, currency=Currency.USD)
        assert m.cents == 12345
        assert m.currency == Currency.USD

    def test_construct_from_units_usd(self):
        m = Money.from_units(100, Currency.USD)
        assert m.cents == 10000  # $100 = 10000 cents

    def test_construct_from_units_jpy(self):
        m = Money.from_units(1000, Currency.JPY)
        assert m.cents == 1000  # JPY has minor_units=0

    def test_construct_from_units_usdc(self):
        m = Money.from_units(1, Currency.USDC)
        assert m.cents == 1_000_000  # USDC has minor_units=6

    def test_zero_money(self):
        m = Money.zero(Currency.JPY)
        assert m.cents == 0
        assert m.is_zero()

    def test_float_cents_rejected(self):
        # bool is subclass of int in Python, so this catches floats specifically
        with pytest.raises(TypeError, match="must be int"):
            Money(cents=1.5, currency=Currency.USD)  # type: ignore

    def test_addition_same_currency(self):
        a = Money(cents=100, currency=Currency.USD)
        b = Money(cents=200, currency=Currency.USD)
        assert (a + b).cents == 300

    def test_addition_different_currency_raises(self):
        a = Money(cents=100, currency=Currency.USD)
        b = Money(cents=200, currency=Currency.JPY)
        with pytest.raises(CurrencyMismatchError):
            a + b

    def test_subtraction(self):
        a = Money(cents=300, currency=Currency.JPY)
        b = Money(cents=100, currency=Currency.JPY)
        assert (a - b).cents == 200

    def test_negation(self):
        a = Money(cents=500, currency=Currency.EUR)
        assert (-a).cents == -500

    def test_immutability(self):
        m = Money(cents=100, currency=Currency.USD)
        # frozen dataclass should reject attribute setting
        with pytest.raises(Exception):
            m.cents = 999  # type: ignore

    def test_comparison(self):
        a = Money(cents=100, currency=Currency.JPY)
        b = Money(cents=200, currency=Currency.JPY)
        assert a < b
        assert b > a
        assert a <= a
        assert a >= a
        assert not (a > b)

    def test_str_jpy(self):
        m = Money.from_units(100000, Currency.JPY)
        assert "JPY" in str(m)
        assert "100,000" in str(m)

    def test_str_usd(self):
        m = Money(cents=12345, currency=Currency.USD)
        assert "123.45 USD" == str(m)

    def test_str_negative(self):
        m = Money(cents=-12345, currency=Currency.USD)
        assert "-123.45 USD" == str(m)


# ---------- Account ----------


class TestAccount:
    def test_new_account_has_unique_id(self):
        a = Account.new(OwnerType.USER, Currency.JPY, "user1")
        b = Account.new(OwnerType.USER, Currency.JPY, "user2")
        assert a.account_id != b.account_id

    def test_account_immutable(self):
        a = Account.new(OwnerType.USER, Currency.JPY, "user")
        with pytest.raises(Exception):
            a.label = "changed"  # type: ignore


# ---------- Transaction ----------


class TestTransaction:
    def _make_balanced_entries(self, tx_id: str):
        return (
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="acc_a",
                amount=Money(cents=-1000, currency=Currency.JPY),
                sequence=0,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="acc_b",
                amount=Money(cents=1000, currency=Currency.JPY),
                sequence=1,
            ),
        )

    def test_balanced_transaction_is_accepted(self):
        from datetime import datetime, timezone

        tx_id = new_transaction_id()
        tx = Transaction(
            transaction_id=tx_id,
            entries=self._make_balanced_entries(tx_id),
            purpose_code="TEST",
            initiator_id="system",
            initiated_at=datetime.now(timezone.utc),
            settled_at=None,
            status=TransactionStatus.PENDING,
            external_refs=(),
            compliance_decision=None,
            metadata=(),
        )
        assert len(tx.entries) == 2

    def test_unbalanced_transaction_rejected(self):
        from datetime import datetime, timezone

        tx_id = new_transaction_id()
        bad_entries = (
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="acc_a",
                amount=Money(cents=-1000, currency=Currency.JPY),
                sequence=0,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="acc_b",
                amount=Money(cents=900, currency=Currency.JPY),  # 不一致
                sequence=1,
            ),
        )
        with pytest.raises(UnbalancedTransactionError):
            Transaction(
                transaction_id=tx_id,
                entries=bad_entries,
                purpose_code="TEST",
                initiator_id="system",
                initiated_at=datetime.now(timezone.utc),
                settled_at=None,
                status=TransactionStatus.PENDING,
                external_refs=(),
                compliance_decision=None,
                metadata=(),
            )

    def test_multi_currency_balance_per_currency(self):
        """各通貨ごとに合計が0でなければならない（FX_GAIN_LOSSが両通貨でバッファ）。"""
        from datetime import datetime, timezone

        tx_id = new_transaction_id()
        # JPY側: -10000 + 10000 = 0 ✓
        # USD側: -100 + 100 = 0 ✓
        entries = (
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="user_jpy",
                amount=Money(cents=-10000, currency=Currency.JPY),
                sequence=0,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="fx_buffer",
                amount=Money(cents=10000, currency=Currency.JPY),
                sequence=1,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="fx_buffer",
                amount=Money(cents=-100, currency=Currency.USD),
                sequence=2,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=tx_id,
                account_id="user_usd",
                amount=Money(cents=100, currency=Currency.USD),
                sequence=3,
            ),
        )
        tx = Transaction(
            transaction_id=tx_id,
            entries=entries,
            purpose_code="FX_CONVERT",
            initiator_id="system",
            initiated_at=datetime.now(timezone.utc),
            settled_at=None,
            status=TransactionStatus.PENDING,
            external_refs=(),
            compliance_decision=None,
            metadata=(),
        )
        assert len(tx.entries) == 4

    def test_entry_with_wrong_tx_id_rejected(self):
        from datetime import datetime, timezone

        tx_id = new_transaction_id()
        wrong_id = new_transaction_id()
        bad_entries = (
            Entry(
                entry_id=new_entry_id(),
                transaction_id=wrong_id,  # 違うID
                account_id="a",
                amount=Money(cents=-100, currency=Currency.JPY),
                sequence=0,
            ),
            Entry(
                entry_id=new_entry_id(),
                transaction_id=wrong_id,
                account_id="b",
                amount=Money(cents=100, currency=Currency.JPY),
                sequence=1,
            ),
        )
        with pytest.raises(IntegrityError):
            Transaction(
                transaction_id=tx_id,
                entries=bad_entries,
                purpose_code="TEST",
                initiator_id="system",
                initiated_at=datetime.now(timezone.utc),
                settled_at=None,
                status=TransactionStatus.PENDING,
                external_refs=(),
                compliance_decision=None,
                metadata=(),
            )
