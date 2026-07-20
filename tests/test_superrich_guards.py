"""tests/test_superrich_guards.py — portfolio / audit as-of / import 規律 / 決定論ガード。"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import mandatehub.core.ledger as ledger_mod
import mandatehub.core.storage as storage_mod
import mandatehub.transparency.audit_log as audit_mod
from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.storage import SQLiteLedgerStorage
from mandatehub.core.types import Currency, Money, OwnerType
from mandatehub.execution import SolverBid, SurplusSplitPolicy, ExecutionAccounts, run_auction
from mandatehub.intent import (
    IntentSettlementEngine,
    MandatePortfolioProofGenerator,
    ProofOfMandateGenerator,
    audit_root_as_of,
)
from mandatehub.transparency.audit_log import GENESIS_HASH, AuditLog

REPO = Path(__file__).parent.parent
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _fund(ledger, src, dst, money):
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, money)
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


# ---------- Portfolio ----------


class TestPortfolio:
    def _two_mandates(self, cap1=100, cap2=100, shared_escrow=False):
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        audit = AuditLog(":memory:")
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e1 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e1")
        _fund(ledger, plat, e1, usdc(1000))
        e2 = e1 if shared_escrow else ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e2")
        if not shared_escrow:
            _fund(ledger, plat, e2, usdc(1000))
        pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e1.account_id, budget_cap=usdc(cap1), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        eng.create_mandate(mandate_id="m2", principal_id="p", escrow_account_id=e2.account_id, budget_cap=usdc(cap2), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        return ledger, eng, e1, e2, pa

    def test_all_within_budget(self):
        ledger, eng, e1, e2, pa = self._two_mandates()
        eng.settle_intent(mandate_id="m1", intent_id="i", payee_account_id=pa.account_id, amount=usdc(30), purpose="A", at=T)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        assert proof.all_within_budget
        assert proof.mandate_count == 2

    def test_shared_escrow_counted_once(self):
        ledger, eng, e1, e2, pa = self._two_mandates(shared_escrow=True)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        # shared escrow (1000) counted once, not 2000
        assert proof.total_escrow_balance_cents == usdc(1000).cents

    def test_under_collateralized_flagged_no_raise(self):
        # 共有 escrow を薄くして remaining 合計 > escrow にする
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e")
        _fund(ledger, plat, e, usdc(100))
        eng = IntentSettlementEngine(ledger, audit_log=AuditLog(":memory:"))
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        # drain escrow below remaining
        plat2 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat2")
        d = TransactionBuilder("WITHDRAW", "ops", initiated_at=T)
        d.transfer(e.account_id, plat2.account_id, usdc(60))
        ledger.post(d.build())
        ledger.settle(d.transaction_id, settled_at=T)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1"], snapshot_at=T, currency=Currency.USDC)
        assert proof.is_collateralized is False  # escrow 40 < remaining 100
        assert proof.all_within_budget is True  # budget itself not exceeded

    def test_independent_escrows_not_netted(self):
        # M1 escrow drained to 0 (remaining 100); M2 overfunded to 200 (remaining 100).
        # A global net (200 >= 200) would falsely say collateralized; per-escrow must not.
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e1 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e1")
        e2 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e2")
        _fund(ledger, plat, e1, usdc(100))
        _fund(ledger, plat, e2, usdc(100))
        eng = IntentSettlementEngine(ledger, audit_log=AuditLog(":memory:"))
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e1.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        eng.create_mandate(mandate_id="m2", principal_id="p", escrow_account_id=e2.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        # drain e1 -> 0, overfund e2 -> 200
        d = TransactionBuilder("WITHDRAW", "ops", initiated_at=T)
        d.transfer(e1.account_id, plat.account_id, usdc(100))
        ledger.post(d.build())
        ledger.settle(d.transaction_id, settled_at=T)
        _fund(ledger, plat, e2, usdc(100))
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        assert proof.total_escrow_balance_cents == usdc(200).cents
        assert proof.total_remaining_cents == usdc(200).cents
        assert proof.is_collateralized is False  # e1 (0) cannot back m1 (100), despite the global net

    def test_currency_mismatch_rejected(self):
        ledger, eng, e1, e2, pa = self._two_mandates()
        with pytest.raises(Exception):
            MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.JPY)


# ---------- audit_root_as_of ----------


class TestAuditAsOf:
    def test_excludes_events_after_snapshot(self):
        audit = AuditLog(":memory:")
        audit.append("e1", {"x": 1}, timestamp=T)
        h1 = audit.latest_hash()
        audit.append("e2", {"x": 2}, timestamp=T + timedelta(hours=2))
        # as-of T excludes e2
        assert audit_root_as_of(audit, T) == h1
        assert audit_root_as_of(audit, T + timedelta(hours=3)) == audit.latest_hash()

    def test_pure_function(self):
        audit = AuditLog(":memory:")
        audit.append("e1", {"x": 1}, timestamp=T)
        assert audit_root_as_of(audit, T) == audit_root_as_of(audit, T)

    def test_none_is_genesis(self):
        assert audit_root_as_of(None, T) == GENESIS_HASH


# ---------- Import discipline (execution must not import intent) ----------


class TestImportDiscipline:
    def test_execution_does_not_import_intent(self):
        exec_dir = REPO / "mandatehub" / "execution"
        offenders = []
        for py in exec_dir.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mandatehub.intent"):
                    offenders.append((py.name, node.module))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("mandatehub.intent"):
                            offenders.append((py.name, alias.name))
        assert offenders == [], f"execution imports intent: {offenders}"


# ---------- No wall clock / no total_seconds in new deterministic modules ----------


def _called_attrs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


NEW_MODULES = [
    "mandatehub/execution/routing.py",
    "mandatehub/execution/auction.py",
    "mandatehub/execution/surplus.py",
    "mandatehub/execution/arbitrage.py",
    "mandatehub/execution/proofs.py",
    "mandatehub/intent/policy.py",
    "mandatehub/intent/settlement.py",
    "mandatehub/intent/lifecycle.py",
    "mandatehub/intent/submandate.py",
    "mandatehub/intent/mandate.py",
    "mandatehub/intent/bridge.py",
    "mandatehub/intent/proofs.py",
    "mandatehub/intent/audit_asof.py",
    "mandatehub/transparency/audit_query.py",
]


class TestNoWallClockStatic:
    def test_no_now_or_total_seconds_calls(self):
        for rel in NEW_MODULES:
            called = _called_attrs(REPO / rel)
            assert "now" not in called, f"{rel} calls .now() (wall-clock leak)"
            assert "total_seconds" not in called, f"{rel} calls .total_seconds() (float)"


# ---------- Determinism + no-wall-clock runtime guard ----------


class _NowForbidden:
    @staticmethod
    def now(*a, **k):
        raise AssertionError("datetime.now() called on a deterministic path (wall-clock leak)")

    fromisoformat = staticmethod(datetime.fromisoformat)
    max = datetime.max
    min = datetime.min


class TestDeterminismRuntime:
    def _run_auction_flow(self):
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        audit = AuditLog(":memory:")
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
        _fund(ledger, plat, escrow, usdc(1000))
        payee = ledger.open_account(OwnerType.USER, Currency.USDC, "payee")
        rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "rebate")
        margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "margin")
        gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        eng.create_mandate(mandate_id="m", principal_id="agent", escrow_account_id=escrow.account_id, budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]), valid_from=T, valid_until=END, created_at=T)
        accts = ExecutionAccounts(payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id, operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id)
        auc = run_auction([SolverBid("s1", "i1", usdc(90).cents, 0, 0)], objective="MIN_COST")
        eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=auc, split_policy=SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000), accounts=accts)
        return eng

    def test_reproducible_proof(self):
        eng = self._run_auction_flow()
        p1, t1 = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
        p2, t2 = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
        assert p1.to_public_summary() == p2.to_public_summary()
        assert t1.root_hash == t2.root_hash

    def test_no_wallclock_across_auction_and_proofs(self):
        orig = (ledger_mod.datetime, storage_mod.datetime, audit_mod.datetime)
        ledger_mod.datetime = _NowForbidden  # type: ignore[assignment]
        storage_mod.datetime = _NowForbidden  # type: ignore[assignment]
        audit_mod.datetime = _NowForbidden  # type: ignore[assignment]
        try:
            eng = self._run_auction_flow()
            proof, _ = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
            assert proof.is_within_budget
            ok, _ = eng.audit_log.verify_chain()
            assert ok
        finally:
            ledger_mod.datetime, storage_mod.datetime, audit_mod.datetime = orig
