"""core/ledger.py に対するテスト。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import (
    ComplianceDecision,
    Currency,
    IntegrityError,
    Money,
    OwnerType,
    TransactionStatus,
    UnbalancedTransactionError,
)


@pytest.fixture
def ledger():
    storage = SQLiteLedgerStorage(":memory:")
    yield Ledger(storage)
    storage.close()


# ---------- 口座管理 ----------


class TestAccountManagement:
    def test_open_and_load_account(self, ledger: Ledger):
        acc = ledger.open_account(
            owner_type=OwnerType.USER, currency=Currency.JPY, label="alice"
        )
        loaded = ledger.get_account(acc.account_id)
        assert loaded.account_id == acc.account_id
        assert loaded.label == "alice"
        assert loaded.currency == Currency.JPY

    def test_list_accounts_filter_by_owner(self, ledger: Ledger):
        ledger.open_account(OwnerType.USER, Currency.JPY, "u1")
        ledger.open_account(OwnerType.USER, Currency.JPY, "u2")
        ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r1")

        users = ledger.list_accounts(owner_type=OwnerType.USER)
        reserves = ledger.list_accounts(owner_type=OwnerType.RESERVE)
        assert len(users) == 2
        assert len(reserves) == 1

    def test_account_with_regulatory_tags(self, ledger: Ledger):
        acc = ledger.open_account(
            OwnerType.USER,
            Currency.JPY,
            "alice",
            regulatory_tags=frozenset(["KYC_LEVEL_2", "JAPAN_RESIDENT"]),
        )
        loaded = ledger.get_account(acc.account_id)
        assert "KYC_LEVEL_2" in loaded.regulatory_tags
        assert "JAPAN_RESIDENT" in loaded.regulatory_tags


# ---------- 単純送金 ----------


class TestSimpleTransfer:
    def test_jpy_transfer_balances_correctly(self, ledger: Ledger):
        alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
        bob = ledger.open_account(OwnerType.USER, Currency.JPY, "bob")
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "deposit_pool")

        # まずプラットフォームからアリスへ初期残高を付与
        builder = TransactionBuilder(purpose_code="INITIAL_DEPOSIT", initiator_id="system")
        builder.transfer(
            from_account_id=platform.account_id,
            to_account_id=alice.account_id,
            amount=Money.from_units(100000, Currency.JPY),
        )
        tx = builder.build()
        ledger.post(tx)
        ledger.settle(tx.transaction_id)

        # アリスの残高 = 10万円
        alice_balance = ledger.balance(alice.account_id)
        assert alice_balance.cents == 100000

        # アリス→ボブの送金
        builder2 = TransactionBuilder(purpose_code="P2P_TRANSFER", initiator_id="alice")
        builder2.transfer(
            from_account_id=alice.account_id,
            to_account_id=bob.account_id,
            amount=Money.from_units(30000, Currency.JPY),
        )
        tx2 = builder2.build()
        ledger.post(tx2)
        ledger.settle(tx2.transaction_id)

        assert ledger.balance(alice.account_id).cents == 70000
        assert ledger.balance(bob.account_id).cents == 30000
        # プラットフォーム口座は -100000（負債側）
        assert ledger.balance(platform.account_id).cents == -100000

    def test_pending_not_in_settled_balance(self, ledger: Ledger):
        a = ledger.open_account(OwnerType.USER, Currency.JPY, "a")
        b = ledger.open_account(OwnerType.USER, Currency.JPY, "b")

        builder = TransactionBuilder(purpose_code="TEST", initiator_id="x")
        builder.transfer(a.account_id, b.account_id, Money.from_units(1000, Currency.JPY))
        tx = builder.build()
        ledger.post(tx)
        # settle していない

        # SETTLED残高には影響しない
        assert ledger.balance(b.account_id).cents == 0
        # PENDING含む残高には影響する
        assert ledger.balance(b.account_id, include_pending=True).cents == 1000

    def test_currency_mismatch_rejected_at_post(self, ledger: Ledger):
        jpy_acc = ledger.open_account(OwnerType.USER, Currency.JPY, "j")
        usd_acc = ledger.open_account(OwnerType.USER, Currency.USD, "u")

        # JPY エントリーを USD 口座に貼ろうとする
        builder = TransactionBuilder(purpose_code="BAD", initiator_id="x")
        builder.add_entry(jpy_acc.account_id, Money(cents=-100, currency=Currency.JPY))
        builder.add_entry(usd_acc.account_id, Money(cents=100, currency=Currency.JPY))  # USD口座にJPY
        tx = builder.build()
        with pytest.raises(UnbalancedTransactionError):
            ledger.post(tx)


# ---------- FX変換 ----------


class TestFXConversion:
    def test_jpy_to_usd_conversion(self, ledger: Ledger):
        # 通貨レート: 1 USD = 150 JPY と仮定
        # 15000 JPY -> 100 USD
        jpy_acc = ledger.open_account(OwnerType.USER, Currency.JPY, "alice_jpy")
        usd_acc = ledger.open_account(OwnerType.USER, Currency.USD, "alice_usd")
        platform_jpy = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p_jpy")
        platform_usd = ledger.open_account(OwnerType.PLATFORM, Currency.USD, "p_usd")
        fx_jpy = ledger.open_account(OwnerType.FX_GAIN_LOSS, Currency.JPY, "fx_jpy_buffer")
        fx_usd = ledger.open_account(OwnerType.FX_GAIN_LOSS, Currency.USD, "fx_usd_buffer")

        # Aliceに15000円を入金
        b1 = TransactionBuilder("INITIAL_DEPOSIT", "system")
        b1.transfer(platform_jpy.account_id, jpy_acc.account_id, Money.from_units(15000, Currency.JPY))
        ledger.post(b1.build())
        ledger.settle(b1.transaction_id)

        # FX変換: JPY 15000 -> USD 100
        # JPY側: alice_jpy -15000, fx_jpy +15000
        # USD側: fx_usd -10000 (cents), platform_usd +10000 (cents)
        # ※ FX_GAIN_LOSS口座は通貨ごとに別物として動く必要があるため、JPY用とUSD用を分ける。
        #    ここではfx_jpy_bufferとfx_usd_bufferの2口座でバッファする。
        b2 = TransactionBuilder("FX_CONVERT", "alice")
        b2.add_entry(jpy_acc.account_id, Money(cents=-15000, currency=Currency.JPY))
        b2.add_entry(fx_jpy.account_id, Money(cents=15000, currency=Currency.JPY))
        b2.add_entry(fx_usd.account_id, Money(cents=-10000, currency=Currency.USD))  # -100ドル
        b2.add_entry(usd_acc.account_id, Money(cents=10000, currency=Currency.USD))  # +100ドル
        # USD流入は platform_usd から提供されたという前提。今回はテスト簡略化のため、
        # FXバッファに USD が予め存在する想定（実環境では事前準備必要）。
        # まず USDバッファに USD 入れる必要があるので、別トランザクションで初期化:

        # USD バッファに 100 ドルを事前注入
        b_init = TransactionBuilder("FX_BUFFER_INIT", "system")
        b_init.transfer(platform_usd.account_id, fx_usd.account_id, Money.from_units(100, Currency.USD))
        ledger.post(b_init.build())
        ledger.settle(b_init.transaction_id)

        ledger.post(b2.build())
        ledger.settle(b2.transaction_id)

        # 検証
        assert ledger.balance(jpy_acc.account_id).cents == 0  # 15000 - 15000
        assert ledger.balance(usd_acc.account_id).cents == 10000  # 100 USD = 10000 cents
        # FXバッファ: JPY側 +15000、USD側 0（-100ドル受け取って+100ドル供給したので0）
        assert ledger.balance(fx_jpy.account_id).cents == 15000
        assert ledger.balance(fx_usd.account_id).cents == 0


# ---------- Append-only / Reversal ----------


class TestAppendOnly:
    def test_cannot_save_same_transaction_twice(self, ledger: Ledger):
        a = ledger.open_account(OwnerType.USER, Currency.JPY, "a")
        b = ledger.open_account(OwnerType.USER, Currency.JPY, "b")

        builder = TransactionBuilder("TEST", "x")
        builder.transfer(a.account_id, b.account_id, Money.from_units(100, Currency.JPY))
        tx = builder.build()
        ledger.post(tx)
        with pytest.raises(IntegrityError, match="already exists"):
            ledger.post(tx)

    def test_reversal_creates_inverse_transaction(self, ledger: Ledger):
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
        bob = ledger.open_account(OwnerType.USER, Currency.JPY, "bob")

        # 初期: アリスに10万円
        b0 = TransactionBuilder("INIT", "sys")
        b0.transfer(platform.account_id, alice.account_id, Money.from_units(100000, Currency.JPY))
        ledger.post(b0.build())
        ledger.settle(b0.transaction_id)

        # アリス→ボブに3万円
        b1 = TransactionBuilder("TRANSFER", "alice")
        b1.transfer(alice.account_id, bob.account_id, Money.from_units(30000, Currency.JPY))
        tx1 = b1.build()
        ledger.post(tx1)
        ledger.settle(tx1.transaction_id)
        assert ledger.balance(alice.account_id).cents == 70000
        assert ledger.balance(bob.account_id).cents == 30000

        # 反対仕訳
        reversal = ledger.reverse(tx1.transaction_id, reason="user_error", initiator_id="admin")

        # 残高が元に戻る
        assert ledger.balance(alice.account_id).cents == 100000
        assert ledger.balance(bob.account_id).cents == 0

        # 元のトランザクションは消えていない
        original = ledger.get_transaction(tx1.transaction_id)
        assert original.status == TransactionStatus.SETTLED
        # 反対仕訳トランザクションは別物として存在
        rev = ledger.get_transaction(reversal.transaction_id)
        assert rev.status == TransactionStatus.SETTLED
        assert "REVERSAL_OF" in rev.purpose_code


# ---------- イベントリスナ ----------


class TestEventListener:
    def test_listener_receives_events(self, ledger: Ledger):
        events: list[tuple[str, dict]] = []

        def listener(event_type: str, payload: dict):
            events.append((event_type, payload))

        ledger.add_listener(listener)
        a = ledger.open_account(OwnerType.USER, Currency.JPY, "a")
        b = ledger.open_account(OwnerType.USER, Currency.JPY, "b")

        builder = TransactionBuilder("TEST", "x")
        builder.transfer(a.account_id, b.account_id, Money.from_units(100, Currency.JPY))
        tx = builder.build()
        ledger.post(tx)
        ledger.settle(tx.transaction_id)

        event_types = [e[0] for e in events]
        assert "account_opened" in event_types
        assert "transaction_posted" in event_types
        assert "transaction_settled" in event_types

    def test_listener_exception_does_not_break_ledger(self, ledger: Ledger):
        def bad_listener(event_type: str, payload: dict):
            raise RuntimeError("intentional crash")

        ledger.add_listener(bad_listener)
        # 例外があっても元帳の動作は継続する
        acc = ledger.open_account(OwnerType.USER, Currency.JPY, "a")
        assert acc.account_id is not None


# ---------- 集約 ----------


class TestAggregation:
    def test_aggregate_user_liabilities(self, ledger: Ledger):
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
        bob = ledger.open_account(OwnerType.USER, Currency.JPY, "bob")
        carol = ledger.open_account(OwnerType.USER, Currency.JPY, "carol")

        for user, amount in [(alice, 10000), (bob, 25000), (carol, 50000)]:
            b = TransactionBuilder("INIT", "sys")
            b.transfer(platform.account_id, user.account_id, Money.from_units(amount, Currency.JPY))
            ledger.post(b.build())
            ledger.settle(b.transaction_id)

        total = ledger.aggregate_by(OwnerType.USER, Currency.JPY)
        assert total.cents == 85000
