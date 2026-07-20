"""
intent/proofs.py — 委任枠の証明（enriched ProofOfMandate + ポートフォリオ証明）。

決定論規律：snapshot_at は必須（ウォールクロック不使用）。監査コミットメントは
audit_root_as_of(snapshot_at)（latest_hash() ではなく as-of）。集計はすべて元帳から
構造的に再導出（別カウンタなし）。

二平面の規律：
- 予算側フィールド（total_settled/remaining/is_within_budget/is_collateralized/
  escrow_balance/settlement_count）は escrow 流出（authorized_outflow）から導出。
  settle_intent と settle_via_auction で byte-identical（INV-9）。
- payee_receipts は payee への実受領（payee_receipt）を attest する（auction では
  legitimately 異なる）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from mandatehub.intent.errors import MandateError
from mandatehub.intent.mandate import IntentSettlementEngine
from mandatehub.intent.settlement import iter_settlement_records
from mandatehub.intent.submandate import descendant_ids
from mandatehub.transparency.audit_query import audit_root_as_of
from mandatehub.transparency.merkle import MerkleLeaf, MerkleTree

_EMPTY_LEAF_ID = "__empty__"


def _tree_from(items: list[tuple[str, int, str]]) -> MerkleTree:
    """(account_id, cents, currency_code) のリスト（呼び出し側で決定論順）から Merkle。"""
    leaves = [MerkleLeaf(account_id=a, balance_cents=c, currency_code=cc) for a, c, cc in items]
    if not leaves:
        leaves = [MerkleLeaf(account_id=_EMPTY_LEAF_ID, balance_cents=0, currency_code="")]
    return MerkleTree(leaves)


@dataclass(frozen=True)
class ProofOfMandate:
    snapshot_at: datetime
    mandate_id: str
    principal_id: str
    currency_code: str
    budget_cap_cents: int
    total_settled_cents: int
    remaining_cents: int
    settlement_count: int
    payee_count: int
    escrow_account_id: str
    escrow_balance_cents: int
    is_within_budget: bool
    is_collateralized: bool
    payee_receipts_root: str
    valid_from: datetime
    valid_until: datetime
    audit_log_root_hash: str
    # --- enriched（既定値つき。既存の利用と互換） ---
    per_epoch_spend: tuple[tuple[int, int], ...] = ()
    remaining_epoch_cap_cents: int | None = None
    remaining_window_cap_cents: int | None = None
    remaining_epoch_velocity: int | None = None
    remaining_window_velocity: int | None = None
    sub_mandate_ids: tuple[str, ...] = ()
    session_tree_root: str | None = None
    aggregate_settled_incl_descendants_cents: int = 0
    lifecycle_state: str = "ACTIVE"
    effective_budget_cap_cents: int = 0
    co_escrow_remaining_cents: int = 0

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "mandate_id": self.mandate_id,
            "principal_id": self.principal_id,
            "currency": self.currency_code,
            "budget_cap_cents": self.budget_cap_cents,
            "effective_budget_cap_cents": self.effective_budget_cap_cents,
            "total_settled_cents": self.total_settled_cents,
            "remaining_cents": self.remaining_cents,
            "settlement_count": self.settlement_count,
            "payee_count": self.payee_count,
            "escrow_account_id": self.escrow_account_id,
            "escrow_balance_cents": self.escrow_balance_cents,
            "is_within_budget": self.is_within_budget,
            "is_collateralized": self.is_collateralized,
            "payee_receipts_merkle_root": self.payee_receipts_root,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "lifecycle_state": self.lifecycle_state,
            "per_epoch_spend": [list(x) for x in self.per_epoch_spend],
            "remaining_epoch_cap_cents": self.remaining_epoch_cap_cents,
            "remaining_window_cap_cents": self.remaining_window_cap_cents,
            "remaining_epoch_velocity": self.remaining_epoch_velocity,
            "remaining_window_velocity": self.remaining_window_velocity,
            "sub_mandate_ids": list(self.sub_mandate_ids),
            "session_tree_root": self.session_tree_root,
            "aggregate_settled_incl_descendants_cents": self.aggregate_settled_incl_descendants_cents,
            "co_escrow_remaining_cents": self.co_escrow_remaining_cents,
            "audit_log_committed": self.audit_log_root_hash,
        }


class ProofOfMandateGenerator:
    def __init__(self, engine: IntentSettlementEngine) -> None:
        self._engine = engine

    def generate(
        self, mandate_id: str, snapshot_at: datetime
    ) -> tuple[ProofOfMandate, MerkleTree]:
        eng = self._engine
        mandate = eng.get_mandate(mandate_id)
        leaf = [
            r
            for r in iter_settlement_records(eng.ledger, as_of=snapshot_at)
            if r.mandate_id == mandate_id
        ]

        total_settled = sum(r.authorized_outflow_cents for r in leaf)
        subtree_settled = eng.subtree_settled_cents(mandate_id, snapshot_at)
        view = eng.mandate_state(mandate_id, snapshot_at)
        effective_cap = view.effective_budget_cap_cents
        remaining = effective_cap - subtree_settled
        escrow_balance = eng.escrow_balance_cents(mandate_id, snapshot_at)
        co_escrow_remaining = eng.co_escrow_remaining_cents(mandate_id, snapshot_at)

        # payee 受領（受領平面）
        receipts: dict[str, int] = {}
        rcys: dict[str, str] = {}
        for r in leaf:
            receipts[r.payee_account_id] = receipts.get(r.payee_account_id, 0) + r.payee_receipt_cents
            rcys[r.payee_account_id] = r.payee_receipt_currency_code
        payee_items = [
            (p, receipts[p], rcys.get(p, mandate.currency.code)) for p in sorted(receipts)
        ]
        tree = _tree_from(payee_items)

        # サブ委任枠 / セッションツリー
        sub_ids = tuple(sorted(descendant_ids(eng.mandates, mandate_id) - {mandate_id}))
        session_root = None
        if sub_ids:
            node_items = []
            for node in sorted(descendant_ids(eng.mandates, mandate_id)):
                node_items.append(
                    ("mandate:" + node, eng.subtree_settled_cents(node, snapshot_at), mandate.currency.code)
                )
            session_root = _tree_from(node_items).root_hash

        # epoch / window の残
        pol = mandate.spend_policy
        per_epoch: tuple[tuple[int, int], ...] = ()
        rem_epoch_cap = rem_window_cap = rem_epoch_vel = rem_window_vel = None
        if pol is not None and pol.epoch is not None:
            spend_by_epoch: dict[int, int] = {}
            for r in leaf:
                idx = pol.epoch.epoch_index(r.settled_at)
                spend_by_epoch[idx] = spend_by_epoch.get(idx, 0) + r.authorized_outflow_cents
            per_epoch = tuple(sorted(spend_by_epoch.items()))
            cur = pol.epoch.epoch_index(snapshot_at)
            if pol.epoch_spend_cap_cents is not None:
                rem_epoch_cap = pol.epoch_spend_cap_cents - spend_by_epoch.get(cur, 0)
            if pol.epoch_settlement_cap is not None:
                cnt = sum(1 for r in leaf if pol.epoch.epoch_index(r.settled_at) == cur)
                rem_epoch_vel = pol.epoch_settlement_cap - cnt
        if pol is not None and pol.rolling_window_seconds is not None:
            lo = snapshot_at - timedelta(seconds=pol.rolling_window_seconds)
            win = [r for r in leaf if lo <= r.settled_at <= snapshot_at]
            if pol.rolling_window_spend_cap_cents is not None:
                rem_window_cap = pol.rolling_window_spend_cap_cents - sum(
                    r.authorized_outflow_cents for r in win
                )
            if pol.rolling_window_settlement_cap is not None:
                rem_window_vel = pol.rolling_window_settlement_cap - len(win)

        proof = ProofOfMandate(
            snapshot_at=snapshot_at,
            mandate_id=mandate_id,
            principal_id=mandate.principal_id,
            currency_code=mandate.currency.code,
            budget_cap_cents=mandate.budget_cap.cents,
            total_settled_cents=total_settled,
            remaining_cents=remaining,
            settlement_count=len({r.intent_id for r in leaf}),
            payee_count=len(receipts),
            escrow_account_id=mandate.escrow_account_id,
            escrow_balance_cents=escrow_balance,
            is_within_budget=remaining >= 0,
            is_collateralized=escrow_balance >= co_escrow_remaining,
            payee_receipts_root=tree.root_hash,
            valid_from=mandate.valid_from,
            valid_until=mandate.valid_until,
            audit_log_root_hash=audit_root_as_of(eng.audit_log, snapshot_at),
            per_epoch_spend=per_epoch,
            remaining_epoch_cap_cents=rem_epoch_cap,
            remaining_window_cap_cents=rem_window_cap,
            remaining_epoch_velocity=rem_epoch_vel,
            remaining_window_velocity=rem_window_vel,
            sub_mandate_ids=sub_ids,
            session_tree_root=session_root,
            aggregate_settled_incl_descendants_cents=subtree_settled,
            lifecycle_state=view.state.value,
            effective_budget_cap_cents=effective_cap,
            co_escrow_remaining_cents=co_escrow_remaining,
        )
        return proof, tree


# ---------- ポートフォリオ証明 ----------


@dataclass(frozen=True)
class MandatePortfolioProof:
    snapshot_at: datetime
    currency_code: str
    mandate_count: int
    portfolio_root: str
    total_budget_cap_cents: int
    total_settled_cents: int
    total_remaining_cents: int
    total_escrow_balance_cents: int  # escrow_account_id で dedupe
    all_within_budget: bool
    is_collateralized: bool
    per_mandate_within_budget: tuple[tuple[str, bool], ...]
    audit_log_root_hash: str

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "currency": self.currency_code,
            "mandate_count": self.mandate_count,
            "portfolio_merkle_root": self.portfolio_root,
            "total_budget_cap_cents": self.total_budget_cap_cents,
            "total_settled_cents": self.total_settled_cents,
            "total_remaining_cents": self.total_remaining_cents,
            "total_escrow_balance_cents": self.total_escrow_balance_cents,
            "all_within_budget": self.all_within_budget,
            "is_collateralized": self.is_collateralized,
            "per_mandate_within_budget": [list(x) for x in self.per_mandate_within_budget],
            "audit_log_committed": self.audit_log_root_hash,
        }


class MandatePortfolioProofGenerator:
    def __init__(self, engine: IntentSettlementEngine) -> None:
        self._engine = engine

    def generate(
        self,
        mandate_ids: Sequence[str],
        snapshot_at: datetime,
        currency,
    ) -> tuple[MandatePortfolioProof, MerkleTree]:
        eng = self._engine
        total_cap = total_settled = total_remaining = 0
        within: list[tuple[str, bool]] = []
        remaining_items: list[tuple[str, int, str]] = []
        # escrow_account_id ごとに balance（1 回）と remaining の合計を持つ。
        # 独立した escrow を横断してネットすると、空になった個別 escrow を過剰資金の
        # 別 escrow が覆い隠してしまう（担保証明の意味が壊れる）。escrow グループ単位で
        # 個別に担保を判定する。
        escrow_balance: dict[str, int] = {}
        escrow_remaining: dict[str, int] = {}

        for mid in mandate_ids:
            m = eng.get_mandate(mid)
            if m.currency != currency:
                raise MandateError(
                    f"portfolio currency {currency.code} != mandate {mid} currency {m.currency.code}"
                )
            cap = eng.effective_cap_cents(mid, snapshot_at)
            settled = eng.subtree_settled_cents(mid, snapshot_at)
            remaining = cap - settled
            total_cap += cap
            total_settled += settled
            total_remaining += remaining
            within.append((mid, remaining >= 0))
            remaining_items.append(("mandate:" + mid, remaining, currency.code))
            eid = m.escrow_account_id
            escrow_remaining[eid] = escrow_remaining.get(eid, 0) + remaining
            if eid not in escrow_balance:
                escrow_balance[eid] = eng.escrow_balance_cents(mid, snapshot_at)

        total_escrow = sum(escrow_balance.values())
        # すべての escrow グループが個別に担保されていること（AND）。
        is_collateralized = all(
            escrow_balance[eid] >= escrow_remaining[eid] for eid in escrow_remaining
        )
        remaining_items.sort()
        tree = _tree_from(remaining_items)
        within.sort()

        proof = MandatePortfolioProof(
            snapshot_at=snapshot_at,
            currency_code=currency.code,
            mandate_count=len(mandate_ids),
            portfolio_root=tree.root_hash,
            total_budget_cap_cents=total_cap,
            total_settled_cents=total_settled,
            total_remaining_cents=total_remaining,
            total_escrow_balance_cents=total_escrow,
            all_within_budget=all(b for _m, b in within),
            is_collateralized=is_collateralized,
            per_mandate_within_budget=tuple(within),
            audit_log_root_hash=audit_root_as_of(eng.audit_log, snapshot_at),
        )
        return proof, tree
