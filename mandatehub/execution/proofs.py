"""
execution/proofs.py — 最良執行証明 / サープラス回収証明。

いずれも決定論的・オフライン検証可能。Merkle は LEAF:/NODE: ドメイン分離を使い、
検証は verify_proof_with_node_prefix でのみ行う（素の MerkleProof.verify() は
既知の壊れた検証器）。監査コミットメントは audit_root_as_of(snapshot_at) を使う。

スコープの正直さ：
- ProofOfBestExecution は「開示された候補集合の中で最良」を証明する。抑圧された
  入札や全ソルバー共謀による一律過少報告は、ライブオラクルなしにはオフラインで
  検出できない（フィールド名 user_no_worse_than_best_disclosed が境界を明示）。
- ProofOfSurplusRecapture の user_effective_fee_vs_limit_non_positive は「利用者自身の
  指値に対して」実質手数料 <= 0 を意味する（フェアミッド対比ではない）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from mandatehub.execution.auction import OBJ_MAX_OUT, AuctionOutcome
from mandatehub.execution.surplus import SplitAllocation, SurplusSplitPolicy, compute_split
from mandatehub.transparency.audit_log import AuditLog
from mandatehub.transparency.audit_query import audit_root_as_of
from mandatehub.transparency.merkle import MerkleLeaf, MerkleTree

_EMPTY_LEAF_ID = "__empty__"


def _merkle_over(items: dict[str, int], currency_code: str, key_prefix: str) -> MerkleTree:
    """{id: cents} から決定論的順序（id 昇順）で Merkle ツリーを作る。空なら placeholder。"""
    leaves = [
        MerkleLeaf(account_id=f"{key_prefix}{k}", balance_cents=v, currency_code=currency_code)
        for k, v in sorted(items.items())
    ]
    if not leaves:
        leaves = [MerkleLeaf(account_id=_EMPTY_LEAF_ID, balance_cents=0, currency_code=currency_code)]
    return MerkleTree(leaves)


def _summary_hash(summary: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------- 最良執行証明 ----------


@dataclass(frozen=True)
class ProofOfBestExecution:
    snapshot_at: datetime
    intent_id: str
    mandate_id: str | None
    in_currency_code: str
    out_currency_code: str
    user_limit_cents: int
    executed_cost_cents: int
    reference_cost_cents: int | None
    candidate_count: int
    candidates_root: str
    winner_id: str
    user_no_worse_than_best_disclosed: bool
    user_within_limit: bool
    split_matches_policy: bool
    audit_log_root_hash: str

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "intent_id": self.intent_id,
            "mandate_id": self.mandate_id,
            "in_currency": self.in_currency_code,
            "out_currency": self.out_currency_code,
            "user_limit_cents": self.user_limit_cents,
            "executed_cost_cents": self.executed_cost_cents,
            "reference_cost_cents": self.reference_cost_cents,
            "candidate_count": self.candidate_count,
            "candidates_merkle_root": self.candidates_root,
            "winner_id": self.winner_id,
            "user_no_worse_than_best_disclosed": self.user_no_worse_than_best_disclosed,
            "user_within_limit": self.user_within_limit,
            "split_matches_policy": self.split_matches_policy,
            "audit_log_committed": self.audit_log_root_hash,
        }

    def artifact_hash(self) -> str:
        return _summary_hash(self.to_public_summary())


class ProofOfBestExecutionGenerator:
    def __init__(self, audit_log: AuditLog | None = None) -> None:
        self._audit_log = audit_log

    def generate(
        self,
        *,
        intent_id: str,
        auction: AuctionOutcome,
        executed_cost_cents: int,
        user_limit_cents: int,
        in_currency_code: str,
        out_currency_code: str,
        split_policy: SurplusSplitPolicy,
        posted_allocation: SplitAllocation,
        surplus_cents: int,
        snapshot_at: datetime,
        mandate_id: str | None = None,
    ) -> tuple[ProofOfBestExecution, MerkleTree]:
        # 開示された全候補（勝者 + 敗者 + 無効入札）で Merkle をコミット。
        # no-worse は auction.objective に沿った軸で判定する（MIN_COST はコスト支配、
        # MAX_OUT は出力支配）。軸を取り違えると MAX_OUT の勝者をコストで誤判定する。
        winner = auction.winner
        candidates = list(auction.losers) + list(auction.invalid)
        if winner is not None:
            candidates.append(winner)
        valid_bids = ([winner] if winner else []) + list(auction.losers)

        if auction.objective == OBJ_MAX_OUT:
            disclosed = {b.solver_id: b.quoted_out_cents for b in candidates}
            no_worse = winner is not None and all(
                winner.quoted_out_cents >= b.quoted_out_cents for b in valid_bids
            )
        else:  # MIN_COST
            disclosed = {b.solver_id: b.fill_cost_cents for b in candidates}
            no_worse = bool(valid_bids) and all(
                executed_cost_cents <= b.fill_cost_cents for b in valid_bids
            )
        tree = _merkle_over(disclosed, in_currency_code, "bid:")
        within_limit = executed_cost_cents <= user_limit_cents

        recomputed = compute_split(surplus_cents, split_policy)
        split_ok = recomputed == posted_allocation

        proof = ProofOfBestExecution(
            snapshot_at=snapshot_at,
            intent_id=intent_id,
            mandate_id=mandate_id,
            in_currency_code=in_currency_code,
            out_currency_code=out_currency_code,
            user_limit_cents=user_limit_cents,
            executed_cost_cents=executed_cost_cents,
            reference_cost_cents=(auction.reference.fill_cost_cents if auction.reference else None),
            candidate_count=len(disclosed),
            candidates_root=tree.root_hash,
            winner_id=(winner.solver_id if winner else ""),
            user_no_worse_than_best_disclosed=no_worse,
            user_within_limit=within_limit,
            split_matches_policy=split_ok,
            audit_log_root_hash=audit_root_as_of(self._audit_log, snapshot_at),
        )
        return proof, tree


# ---------- サープラス回収証明 ----------


@dataclass(frozen=True)
class SurplusEvent:
    """1 件の回収済みサープラス（分配内訳つき）。証明の入力単位。"""

    event_id: str
    surplus_cents: int
    allocation: SplitAllocation


@dataclass(frozen=True)
class ProofOfSurplusRecapture:
    snapshot_at: datetime
    currency_code: str  # C_out（サープラス平面）
    surplus_event_count: int
    surplus_events_root: str
    total_surplus_cents: int
    total_user_rebate_cents: int
    total_operator_margin_cents: int
    total_gas_cents: int
    total_referrer_cents: int
    splits_sum_exact: bool
    user_effective_fee_vs_limit_non_positive: bool
    audit_log_root_hash: str

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "currency": self.currency_code,
            "surplus_event_count": self.surplus_event_count,
            "surplus_events_merkle_root": self.surplus_events_root,
            "total_surplus_cents": self.total_surplus_cents,
            "total_user_rebate_cents": self.total_user_rebate_cents,
            "total_operator_margin_cents": self.total_operator_margin_cents,
            "total_gas_cents": self.total_gas_cents,
            "total_referrer_cents": self.total_referrer_cents,
            "splits_sum_exact": self.splits_sum_exact,
            "user_effective_fee_vs_limit_non_positive": self.user_effective_fee_vs_limit_non_positive,
            "audit_log_committed": self.audit_log_root_hash,
        }

    def artifact_hash(self) -> str:
        return _summary_hash(self.to_public_summary())


class ProofOfSurplusRecaptureGenerator:
    def __init__(self, audit_log: AuditLog | None = None) -> None:
        self._audit_log = audit_log

    def generate(
        self,
        *,
        surplus_events: Sequence[SurplusEvent],
        snapshot_at: datetime,
        currency,
    ) -> tuple[ProofOfSurplusRecapture, MerkleTree]:
        by_id: dict[str, int] = {}
        total_surplus = total_rebate = total_margin = total_gas = total_ref = 0
        splits_exact = True
        eff_fee_non_positive = True
        for ev in surplus_events:
            a = ev.allocation
            by_id[ev.event_id] = ev.surplus_cents
            total_surplus += ev.surplus_cents
            total_rebate += a.user_rebate_cents
            total_margin += a.operator_margin_cents
            total_gas += a.gas_cents
            total_ref += a.referrer_cents
            if a.total() != ev.surplus_cents:
                splits_exact = False
            # 実質手数料（指値対比）= -rebate <= 0 ⇔ rebate >= 0
            if a.user_rebate_cents < 0:
                eff_fee_non_positive = False

        tree = _merkle_over(by_id, currency.code, "surplus:")

        proof = ProofOfSurplusRecapture(
            snapshot_at=snapshot_at,
            currency_code=currency.code,
            surplus_event_count=len(surplus_events),
            surplus_events_root=tree.root_hash,
            total_surplus_cents=total_surplus,
            total_user_rebate_cents=total_rebate,
            total_operator_margin_cents=total_margin,
            total_gas_cents=total_gas,
            total_referrer_cents=total_ref,
            splits_sum_exact=splits_exact,
            user_effective_fee_vs_limit_non_positive=eff_fee_non_positive,
            audit_log_root_hash=audit_root_as_of(self._audit_log, snapshot_at),
        )
        return proof, tree
