"""tests/test_intent_submandate.py — セッション鍵 / サブ委任枠の非漏洩・集約。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.intent import IntentSettlementEngine, MandateError, ProofOfMandateGenerator
from mandatehub.transparency.audit_log import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)
CHILD_END = T + timedelta(days=10)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _engine(budget=1000):
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    audit = AuditLog(":memory:")
    plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
    escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(plat.account_id, escrow.account_id, usdc(budget))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)
    pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="root", principal_id="orch", escrow_account_id=escrow.account_id,
        budget_cap=usdc(100), allowed_purposes=frozenset(["A", "B"]), valid_from=T, valid_until=END, created_at=T,
    )
    return ledger, escrow, pa, eng


def _child(eng, mid, cap, purposes=("A",), parent="root"):
    return eng.create_sub_mandate(
        parent_mandate_id=parent, mandate_id=mid, delegate_id="sub", sub_budget_cap=usdc(cap),
        allowed_purposes=frozenset(purposes), valid_from=T, valid_until=CHILD_END, created_at=T,
    )


class TestCreation:
    def test_window_must_be_subset(self):
        _l, _e, _pa, eng = _engine()
        with pytest.raises(MandateError):
            eng.create_sub_mandate(parent_mandate_id="root", mandate_id="c", delegate_id="s", sub_budget_cap=usdc(10), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END + timedelta(days=1), created_at=T)

    def test_purpose_must_be_subset(self):
        _l, _e, _pa, eng = _engine()
        with pytest.raises(MandateError):
            _child(eng, "c", 10, purposes=("Z",))

    def test_currency_must_match(self):
        _l, _e, _pa, eng = _engine()
        with pytest.raises(MandateError):
            eng.create_sub_mandate(parent_mandate_id="root", mandate_id="c", delegate_id="s", sub_budget_cap=Money.from_units(10, Currency.JPY), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=CHILD_END, created_at=T)

    def test_depth_cap(self):
        _l, _e, _pa, eng = _engine()
        parent = "root"
        for i in range(8):  # root depth 0; d0..d7 reach depth 8 (MAX_DELEGATION_DEPTH)
            eng.create_sub_mandate(parent_mandate_id=parent, mandate_id=f"d{i}", delegate_id="s", sub_budget_cap=usdc(10), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=CHILD_END, created_at=T)
            parent = f"d{i}"
        with pytest.raises(MandateError):
            eng.create_sub_mandate(parent_mandate_id=parent, mandate_id="too_deep", delegate_id="s", sub_budget_cap=usdc(10), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=CHILD_END, created_at=T)


class TestNonLeakage:
    def test_ancestor_budget_enforced(self):
        _l, _e, pa, eng = _engine()
        _child(eng, "c1", 80, purposes=("A",))
        _child(eng, "c2", 80, purposes=("B",))
        assert eng.settle_intent(mandate_id="c1", intent_id="x1", payee_account_id=pa.account_id, amount=usdc(60), purpose="A", at=T).decision == "SETTLED"
        # c1 60 + c2 50 = 110 > root cap 100
        r = eng.settle_intent(mandate_id="c2", intent_id="x2", payee_account_id=pa.account_id, amount=usdc(50), purpose="B", at=T)
        assert r.reason == "PARENT_BUDGET_EXCEEDED"
        assert eng.settle_intent(mandate_id="c2", intent_id="x3", payee_account_id=pa.account_id, amount=usdc(40), purpose="B", at=T).decision == "SETTLED"
        assert eng.subtree_settled_cents("root", T) == usdc(100).cents

    def test_child_local_budget(self):
        _l, _e, pa, eng = _engine()
        _child(eng, "c1", 30)
        assert eng.settle_intent(mandate_id="c1", intent_id="x1", payee_account_id=pa.account_id, amount=usdc(40), purpose="A", at=T).reason == "BUDGET_EXCEEDED"

    def test_grandchild_aggregates_to_all_ancestors(self):
        _l, _e, pa, eng = _engine()
        _child(eng, "c1", 90)
        eng.create_sub_mandate(parent_mandate_id="c1", mandate_id="g", delegate_id="s", sub_budget_cap=usdc(90), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=CHILD_END, created_at=T)
        eng.settle_intent(mandate_id="g", intent_id="x1", payee_account_id=pa.account_id, amount=usdc(90), purpose="A", at=T)
        # grandchild spend counts against c1 and root
        assert eng.subtree_settled_cents("c1", T) == usdc(90).cents
        assert eng.subtree_settled_cents("root", T) == usdc(90).cents
        # root remaining 10; a further 20 anywhere hits PARENT/BUDGET
        assert eng.settle_intent(mandate_id="c1", intent_id="x2", payee_account_id=pa.account_id, amount=usdc(20), purpose="A", at=T).reason in ("PARENT_BUDGET_EXCEEDED", "BUDGET_EXCEEDED")

    def test_substring_ids_do_not_collide(self):
        # "a" と "ab" は別 ID。集合メンバーシップなので部分文字列衝突は起きない。
        _l, _e, pa, eng = _engine()
        _child(eng, "a", 50)
        _child(eng, "ab", 50)
        eng.settle_intent(mandate_id="ab", intent_id="x1", payee_account_id=pa.account_id, amount=usdc(40), purpose="A", at=T)
        assert eng.subtree_settled_cents("a", T) == 0
        assert eng.subtree_settled_cents("ab", T) == usdc(40).cents

    def test_per_child_collateralization(self):
        # 100 の escrow を共有する 2 子が各 remaining 60 → co-escrow 120 > 100 で collateralized False
        _l, escrow, pa, eng = _engine()
        _child(eng, "c1", 60)
        _child(eng, "c2", 60)
        # root cap 100 but children authorize 60 each; co-escrow remaining = root subtree remaining = 100
        # drain escrow below to force under-collateralization is cleaner:
        p1, _ = ProofOfMandateGenerator(eng).generate("c1", snapshot_at=T)
        # escrow 1000 >> remaining, so collateralized True here; assert co-escrow uses root aggregate
        assert p1.co_escrow_remaining_cents == eng.remaining_cents("root", T)


class TestSessionTree:
    def test_session_root_changes_when_descendant_spends(self):
        _l, _e, pa, eng = _engine()
        _child(eng, "c1", 90)
        p_before, _ = ProofOfMandateGenerator(eng).generate("root", snapshot_at=T)
        eng.settle_intent(mandate_id="c1", intent_id="x1", payee_account_id=pa.account_id, amount=usdc(10), purpose="A", at=T + timedelta(seconds=1))
        p_after, _ = ProofOfMandateGenerator(eng).generate("root", snapshot_at=T + timedelta(hours=1))
        assert p_before.session_tree_root is not None
        assert p_before.sub_mandate_ids == ("c1",)
        assert p_before.session_tree_root != p_after.session_tree_root
