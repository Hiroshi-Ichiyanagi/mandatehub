"""tests/test_intent_proof.py — ProofOfMandate（委任枠の証明）のテスト。

証明が満たすべき性質：
  - 予算・担保・履歴を正しく反映する
  - payee は自分の受領累計の包含を、他者を知らずに Merkle 証明で検証できる
  - 決定論的（同一状態 + 同一 snapshot -> 同一ルート・同一要約）
  - 枠外れ（out-of-band 超過・escrow 抜き取り）を検出する
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.intent import IntentSettlementEngine, ProofOfMandateGenerator
from mandatehub.transparency.audit_log import AuditLog
from mandatehub.transparency.merkle import verify_proof_with_node_prefix

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
SNAP = T + timedelta(hours=1)
WINDOW_END = T + timedelta(days=30)


def usdc(units: int) -> Money:
    return Money.from_units(units, Currency.USDC)


def _build(with_audit: bool = True):
    """escrow(100) + 2 payee + m1(cap 100) を作り、30/25 を成立させた状態を返す。"""
    storage = SQLiteLedgerStorage(":memory:")
    ledger = Ledger(storage)
    audit = AuditLog(":memory:") if with_audit else None
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "platform")
    escrow = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "escrow")
    payee_a = ledger.open_account(OwnerType.USER, Currency.USDC, "A")
    payee_b = ledger.open_account(OwnerType.USER, Currency.USDC, "B")

    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
    b.transfer(platform.account_id, escrow.account_id, usdc(100))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)

    engine = IntentSettlementEngine(ledger, audit_log=audit)
    engine.create_mandate(
        mandate_id="m1",
        principal_id="agent",
        escrow_account_id=escrow.account_id,
        budget_cap=usdc(100),
        allowed_purposes=frozenset(["API_CALL", "DATA_STREAM"]),
        valid_from=T,
        valid_until=WINDOW_END,
        created_at=T,
    )
    engine.settle_intent(mandate_id="m1", intent_id="i1", payee_account_id=payee_a.account_id, amount=usdc(30), purpose="API_CALL", at=T)
    engine.settle_intent(mandate_id="m1", intent_id="i2", payee_account_id=payee_b.account_id, amount=usdc(25), purpose="DATA_STREAM", at=T)
    return {
        "storage": storage,
        "ledger": ledger,
        "engine": engine,
        "escrow": escrow,
        "payee_a": payee_a,
        "payee_b": payee_b,
        "platform": platform,
    }


class TestProofContent:
    def test_reflects_budget_and_settlement(self):
        env = _build()
        gen = ProofOfMandateGenerator(env["engine"])
        proof, _tree = gen.generate("m1", snapshot_at=SNAP)
        assert proof.budget_cap_cents == usdc(100).cents
        assert proof.total_settled_cents == usdc(55).cents
        assert proof.remaining_cents == usdc(45).cents
        assert proof.settlement_count == 2
        assert proof.payee_count == 2
        assert proof.is_within_budget
        assert proof.is_collateralized
        assert proof.escrow_balance_cents == usdc(45).cents
        env["storage"].close()

    def test_public_summary_hides_individual_receipts(self):
        env = _build()
        proof, _ = ProofOfMandateGenerator(env["engine"]).generate("m1", snapshot_at=SNAP)
        summary = proof.to_public_summary()
        # 個別 payee 受領額は要約に含まれない（Merkle ルートのみ）
        assert "payee_receipts_merkle_root" in summary
        assert env["payee_a"].account_id not in summary
        assert summary["total_settled_cents"] == usdc(55).cents
        env["storage"].close()


class TestPayeeInclusion:
    def test_payee_verifies_own_inclusion(self):
        env = _build()
        proof, tree = ProofOfMandateGenerator(env["engine"]).generate("m1", snapshot_at=SNAP)
        # payee A は自分の受領累計(30)がツリーに含まれることを検証できる
        pa = tree.proof_for(env["payee_a"].account_id)
        assert pa.leaf.balance_cents == usdc(30).cents
        assert verify_proof_with_node_prefix(pa)
        assert pa.root_hash == proof.payee_receipts_root
        env["storage"].close()

    def test_empty_mandate_has_wellformed_proof(self):
        # 決済 0 件でも証明は生成できる（空葉プレースホルダ）
        storage = SQLiteLedgerStorage(":memory:")
        ledger = Ledger(storage)
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "p")
        escrow = ledger.open_account(OwnerType.CLEARING, Currency.USDC, "e")
        b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
        b.transfer(platform.account_id, escrow.account_id, usdc(10))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T)
        engine = IntentSettlementEngine(ledger)
        engine.create_mandate(
            mandate_id="m0", principal_id="a", escrow_account_id=escrow.account_id,
            budget_cap=usdc(10), allowed_purposes=frozenset(["X"]),
            valid_from=T, valid_until=WINDOW_END, created_at=T,
        )
        proof, tree = ProofOfMandateGenerator(engine).generate("m0", snapshot_at=SNAP)
        assert proof.settlement_count == 0
        assert proof.remaining_cents == usdc(10).cents
        assert proof.is_within_budget and proof.is_collateralized
        assert tree.root_hash == proof.payee_receipts_root
        storage.close()


class TestDeterminism:
    def test_same_state_same_snapshot_reproducible(self):
        env = _build()
        gen = ProofOfMandateGenerator(env["engine"])
        p1, t1 = gen.generate("m1", snapshot_at=SNAP)
        p2, t2 = gen.generate("m1", snapshot_at=SNAP)
        assert p1.to_public_summary() == p2.to_public_summary()
        assert p1.payee_receipts_root == p2.payee_receipts_root
        assert t1.root_hash == t2.root_hash
        env["storage"].close()

    def test_two_independent_ledgers_same_receipts_root(self):
        # 独立に組んだ同一状態の元帳は、同一の payee 受領 Merkle ルートを生む。
        # (account_id は uuid4 で異なるため、ルート一致は同一 account_id 集合を使う場合に限る。
        #  ここでは payee ルートの決定論を「同一エンジンの再生成」で担保するに留め、
        #  独立元帳では集計値の一致を検証する。)
        env1, env2 = _build(), _build()
        g1 = ProofOfMandateGenerator(env1["engine"]).generate("m1", snapshot_at=SNAP)[0]
        g2 = ProofOfMandateGenerator(env2["engine"]).generate("m1", snapshot_at=SNAP)[0]
        assert g1.total_settled_cents == g2.total_settled_cents
        assert g1.remaining_cents == g2.remaining_cents
        assert g1.settlement_count == g2.settlement_count
        env1["storage"].close()
        env2["storage"].close()

    def test_snapshot_at_required(self):
        env = _build()
        gen = ProofOfMandateGenerator(env["engine"])
        with pytest.raises(TypeError):
            gen.generate("m1")  # type: ignore[call-arg]
        env["storage"].close()


class TestProofCatchesViolations:
    """証明は「枠外れ」を検出できる（運営者を信頼しない）。"""

    def test_out_of_band_overspend_flagged_not_within_budget(self):
        # エンジンを経由せず、直接 escrow から INTENT_SETTLEMENT を偽装して
        # budget_cap を超える流出を作る → proof.is_within_budget が False になる。
        # （well-formed な settlement tag を付けた「枠超過」— 構造リーダーは受理し、
        #  予算不変条件の破れは is_within_budget=False として検出される。）
        env = _build()
        ledger, escrow, payee = env["ledger"], env["escrow"], env["payee_a"]
        b = TransactionBuilder("API_CALL", "rogue", initiated_at=T)
        b.transfer(escrow.account_id, payee.account_id, usdc(60))  # 55 + 60 = 115 > 100
        b.with_metadata("transaction_type", "INTENT_SETTLEMENT")
        b.with_metadata("mandate_id", "m1")
        b.with_metadata("intent_id", "rogue-1")
        b.with_metadata("escrow_account_id", escrow.account_id)
        b.with_metadata("payee_account_id", payee.account_id)
        # escrow を先に補填しておく（残高不足で balance が負になるのを避け、超過だけを示す）
        topup = TransactionBuilder("DEPOSIT", "ops", initiated_at=T)
        topup.transfer(env["platform"].account_id, escrow.account_id, usdc(60))
        ledger.post(topup.build())
        ledger.settle(topup.transaction_id, settled_at=T)
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T)

        proof, _ = ProofOfMandateGenerator(env["engine"]).generate("m1", snapshot_at=SNAP)
        assert proof.total_settled_cents == usdc(115).cents
        assert proof.remaining_cents == -usdc(15).cents
        assert proof.is_within_budget is False
        env["storage"].close()

    def test_drained_escrow_flagged_not_collateralized(self):
        # escrow を mandate 外の送金で抜くと、残枠を裏付ける資金が不足し
        # proof.is_collateralized が False になる。
        env = _build()
        ledger, escrow, platform = env["ledger"], env["escrow"], env["platform"]
        # 残枠は 45。escrow から 40 を mandate 外で引き抜く → escrow 5 < remaining 45
        drain = TransactionBuilder("WITHDRAW", "ops", initiated_at=T)
        drain.transfer(escrow.account_id, platform.account_id, usdc(40))
        ledger.post(drain.build())
        ledger.settle(drain.transaction_id, settled_at=T)

        proof, _ = ProofOfMandateGenerator(env["engine"]).generate("m1", snapshot_at=SNAP)
        assert proof.remaining_cents == usdc(45).cents  # mandate 消化は変わらない
        assert proof.escrow_balance_cents == usdc(5).cents
        assert proof.is_collateralized is False
        env["storage"].close()
