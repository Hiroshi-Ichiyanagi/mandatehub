"""
intent/mandate.py — インテント（意図）ベースの自律決済オーソライゼーション層。

「利用者が大枠の予算枠（Mandate）を信託し、枠内で自律エージェントが決済を
繰り返す」モデルの検証コア。決済実行（オンチェーン bundler / paymaster /
セッション鍵）は範囲外で、ここが担保するのは「枠を一度も超えていない」ことを
第三者がオフラインで検証できることである。

すべての累計（予算・velocity・epoch・sub-budget・nonce・重複）は元帳から
構造的に再導出する（別カウンタを持たない）。成立・却下・ライフサイクルの全判断は
改竄検出可能な監査ログに刻まれる。best-execution / バッチは bridge / settle_batch で
同じ認可プリミティブ（_authorize / _post_settlement）を通す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from mandatehub.core.ledger import Ledger, TransactionBuilder
from mandatehub.core.types import Currency, Money, TransactionStatus
from mandatehub.intent.errors import MandateError
from mandatehub.intent.lifecycle import (
    EVT_PAUSED,
    EVT_RESUMED,
    EVT_REVOKED,
    EVT_TOPPED_UP,
    MandateLifecycleView,
    MandateState,
    fold_lifecycle,
)
from mandatehub.intent.policy import SpendPolicy
from mandatehub.intent.results import (
    BatchSettlementResult,
    IntentRequest,
    IntentSettlementResult,
)
from mandatehub.intent.settlement import (
    KEY_BATCH,
    KEY_EPOCH,
    KEY_ESCROW,
    KEY_INTENT_ID,
    KEY_KIND,
    KEY_MANDATE_ID,
    KEY_MANDATE_PATH,
    KEY_NONCE,
    KEY_PAYEE,
    KEY_ROOT_MANDATE_ID,
    KEY_TXN_TYPE,
    KIND_PLAIN,
    VAL_INTENT_SETTLEMENT,
    SettlementRecord,
    iter_settlement_records,
)
from mandatehub.intent.submandate import (
    MAX_DELEGATION_DEPTH,
    ancestor_ids,
    depth_of,
    descendant_ids,
    mandate_path,
    root_id_of,
)
from mandatehub.transparency.audit_log import AuditLog

# 却下理由の正準順序（最初に失敗したものが返る）。監査に刻まれる契約。
DENIAL_ORDER: tuple[str, ...] = (
    "CURRENCY_MISMATCH",
    "NON_POSITIVE_AMOUNT",
    "MANDATE_REVOKED",
    "MANDATE_EXPIRED",
    "MANDATE_PAUSED",
    "OUTSIDE_WINDOW",
    "NON_MONOTONIC_TIME",
    "PURPOSE_NOT_ALLOWED",
    "PAYEE_NOT_ALLOWED",
    "BELOW_MIN_AMOUNT",
    "ABOVE_MAX_AMOUNT",
    "PER_TX_LIMIT_EXCEEDED",
    "NONCE_REUSED",
    "NONCE_NOT_INCREASING",
    "DUPLICATE_INTENT",
    "SUB_BUDGET_EXCEEDED",
    "EPOCH_VELOCITY_EXCEEDED",
    "WINDOW_VELOCITY_EXCEEDED",
    "EPOCH_CAP_EXCEEDED",
    "WINDOW_CAP_EXCEEDED",
    "PARENT_BUDGET_EXCEEDED",
    "BUDGET_EXCEEDED",
)

# 「これまでに成立した全決済」を読むための as-of 上限（now() は使わない）。
_DATETIME_MAX = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Mandate:
    """自律エージェントに与える「意図（予算枠）」の委任。"""

    mandate_id: str
    principal_id: str
    escrow_account_id: str
    currency: Currency
    budget_cap: Money
    allowed_purposes: frozenset[str]
    valid_from: datetime
    valid_until: datetime
    created_at: datetime
    per_transaction_limit: Money | None = None
    parent_mandate_id: str | None = None
    spend_policy: SpendPolicy | None = None
    nonce_required: bool = False

    def __post_init__(self) -> None:
        if "/" in self.mandate_id:
            raise MandateError("mandate_id must not contain '/'")
        if self.budget_cap.currency != self.currency:
            raise MandateError(
                f"budget_cap currency {self.budget_cap.currency.code} "
                f"!= mandate currency {self.currency.code}"
            )
        if not self.budget_cap.is_positive():
            raise MandateError("budget_cap must be positive")
        if self.per_transaction_limit is not None:
            if self.per_transaction_limit.currency != self.currency:
                raise MandateError("per_transaction_limit currency mismatch")
            if not self.per_transaction_limit.is_positive():
                raise MandateError("per_transaction_limit must be positive")
        if self.valid_until < self.valid_from:
            raise MandateError("valid_until must be >= valid_from")
        if not self.allowed_purposes:
            raise MandateError("allowed_purposes must not be empty")

    def check_static(self, purpose: str, amount: Money, at: datetime) -> tuple[bool, str]:
        """累計に依存しない静的ルール（通貨・符号・有効期間・用途・1件上限）。"""
        if amount.currency != self.currency:
            return False, "CURRENCY_MISMATCH"
        if not amount.is_positive():
            return False, "NON_POSITIVE_AMOUNT"
        if at < self.valid_from or at > self.valid_until:
            return False, "OUTSIDE_WINDOW"
        if purpose not in self.allowed_purposes:
            return False, "PURPOSE_NOT_ALLOWED"
        if self.per_transaction_limit is not None and amount > self.per_transaction_limit:
            return False, "PER_TX_LIMIT_EXCEEDED"
        return True, "OK"


class IntentSettlementEngine:
    """委任枠に基づいてインテントを自律決済するエンジン。"""

    def __init__(self, ledger: Ledger, audit_log: AuditLog | None = None) -> None:
        self._ledger = ledger
        self._audit_log = audit_log
        self._mandates: dict[str, Mandate] = {}

    @property
    def audit_log(self) -> AuditLog | None:
        return self._audit_log

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    @property
    def mandates(self) -> dict[str, Mandate]:
        return dict(self._mandates)

    # ---------- 委任枠の作成 ----------

    def create_mandate(
        self,
        *,
        mandate_id: str,
        principal_id: str,
        escrow_account_id: str,
        budget_cap: Money,
        allowed_purposes: frozenset[str],
        valid_from: datetime,
        valid_until: datetime,
        created_at: datetime,
        per_transaction_limit: Money | None = None,
        spend_policy: SpendPolicy | None = None,
        nonce_required: bool = False,
    ) -> Mandate:
        """ルート委任枠を成立させる（escrow が budget_cap 以上で完全担保のとき）。"""
        if mandate_id in self._mandates:
            raise MandateError(f"mandate already exists: {mandate_id}")
        escrow = self._ledger.get_account(escrow_account_id)
        if escrow.currency != budget_cap.currency:
            raise MandateError(
                f"escrow currency {escrow.currency.code} != budget_cap {budget_cap.currency.code}"
            )
        escrow_balance = self._ledger.balance(escrow_account_id, as_of=created_at)
        if escrow_balance < budget_cap:
            raise MandateError(
                f"under-collateralized mandate: escrow balance {escrow_balance} < budget_cap {budget_cap}."
            )
        mandate = Mandate(
            mandate_id=mandate_id,
            principal_id=principal_id,
            escrow_account_id=escrow_account_id,
            currency=budget_cap.currency,
            budget_cap=budget_cap,
            allowed_purposes=frozenset(allowed_purposes),
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=created_at,
            per_transaction_limit=per_transaction_limit,
            spend_policy=spend_policy,
            nonce_required=nonce_required,
        )
        self._mandates[mandate_id] = mandate
        self._audit(
            "mandate_created",
            {
                KEY_MANDATE_ID: mandate_id,
                "principal_id": principal_id,
                "parent_mandate_id": None,
                "escrow_account_id": escrow_account_id,
                "currency": mandate.currency.code,
                "budget_cap_cents": budget_cap.cents,
                "per_transaction_limit_cents": (
                    per_transaction_limit.cents if per_transaction_limit else None
                ),
                "allowed_purposes": sorted(mandate.allowed_purposes),
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
                "nonce_required": nonce_required,
            },
            at=created_at,
        )
        return mandate

    def create_sub_mandate(
        self,
        *,
        parent_mandate_id: str,
        mandate_id: str,
        delegate_id: str,
        sub_budget_cap: Money,
        allowed_purposes: frozenset[str],
        valid_from: datetime,
        valid_until: datetime,
        created_at: datetime,
        per_transaction_limit: Money | None = None,
        spend_policy: SpendPolicy | None = None,
        nonce_required: bool = False,
    ) -> Mandate:
        """親の escrow を共有するサブ委任枠（セッション鍵）を作る。"""
        if mandate_id in self._mandates:
            raise MandateError(f"mandate already exists: {mandate_id}")
        parent = self.get_mandate(parent_mandate_id)
        if self.mandate_state(parent_mandate_id, created_at).state != MandateState.ACTIVE:
            raise MandateError("parent mandate is not ACTIVE")
        if sub_budget_cap.currency != parent.currency:
            raise MandateError("sub_budget_cap currency must match parent")
        if valid_from < parent.valid_from or valid_until > parent.valid_until:
            raise MandateError("child window must be within parent window")
        if not frozenset(allowed_purposes) <= parent.allowed_purposes:
            raise MandateError("child purposes must be a subset of parent purposes")
        if depth_of(self._mandates, parent_mandate_id) + 1 > MAX_DELEGATION_DEPTH:
            raise MandateError(f"delegation depth exceeds {MAX_DELEGATION_DEPTH}")

        mandate = Mandate(
            mandate_id=mandate_id,
            principal_id=delegate_id,
            escrow_account_id=parent.escrow_account_id,
            currency=parent.currency,
            budget_cap=sub_budget_cap,
            allowed_purposes=frozenset(allowed_purposes),
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=created_at,
            per_transaction_limit=per_transaction_limit,
            parent_mandate_id=parent_mandate_id,
            spend_policy=spend_policy,
            nonce_required=nonce_required,
        )
        self._mandates[mandate_id] = mandate
        self._audit(
            "mandate_created",
            {
                KEY_MANDATE_ID: mandate_id,
                "principal_id": delegate_id,
                "parent_mandate_id": parent_mandate_id,
                "escrow_account_id": parent.escrow_account_id,
                "currency": mandate.currency.code,
                "budget_cap_cents": sub_budget_cap.cents,
                "allowed_purposes": sorted(mandate.allowed_purposes),
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
                "nonce_required": nonce_required,
            },
            at=created_at,
        )
        return mandate

    def get_mandate(self, mandate_id: str) -> Mandate:
        try:
            return self._mandates[mandate_id]
        except KeyError:
            raise MandateError(f"unknown mandate: {mandate_id}") from None

    # ---------- 再起動復元（H2: 永続ストレージからの再構築） ----------

    def rehydrate_mandate(self, mandate: Mandate) -> None:
        """既に台帳・監査チェーンに歴史を持つ委任枠を、再起動したエンジンに再装着する。

        `create_mandate` と違い、監査イベントを追記せず、担保の再検証も行わない
        （どちらも初回作成時に済んでおり、再実行すると歴史が二重になる）。装着後の
        予算・リプレイ・単調時刻・親子集計はすべて台帳／監査チェーンから再導出される
        ので、プロセスを再起動しても認可判定は同一に保たれる。

        fail-closed ガード:
          - 同じ mandate_id が既に装着済みなら拒否（二重装着は設定ミス）。
          - escrow 口座が台帳に存在しなければ拒否（別の台帳への誤装着を検出）。
          - 親を持つ委任枠は、親が先に装着されていなければ拒否（集計が壊れる）。
        """
        if mandate.mandate_id in self._mandates:
            raise MandateError(f"mandate already attached: {mandate.mandate_id}")
        try:
            self._ledger.get_account(mandate.escrow_account_id)
        except Exception:
            raise MandateError(
                f"escrow account not found in this ledger: {mandate.escrow_account_id} "
                "(rehydrating against the wrong ledger?)"
            ) from None
        if mandate.parent_mandate_id is not None and mandate.parent_mandate_id not in self._mandates:
            raise MandateError(
                f"parent mandate must be rehydrated first: {mandate.parent_mandate_id}"
            )
        self._mandates[mandate.mandate_id] = mandate

    # ---------- ライフサイクル ----------

    def mandate_state(self, mandate_id: str, at: datetime) -> MandateLifecycleView:
        mandate = self.get_mandate(mandate_id)
        events = self._audit_log.iter_events() if self._audit_log is not None else ()
        return fold_lifecycle(
            events,
            mandate_id=mandate_id,
            base_budget_cap_cents=mandate.budget_cap.cents,
            valid_until=mandate.valid_until,
            at=at,
        )

    def pause_mandate(self, mandate_id: str, *, at: datetime, reason: str):
        if self.mandate_state(mandate_id, at).state == MandateState.REVOKED:
            raise MandateError("cannot pause a revoked mandate")
        return self._audit(EVT_PAUSED, {KEY_MANDATE_ID: mandate_id, "reason": reason, "at": at.isoformat()}, at=at)

    def resume_mandate(self, mandate_id: str, *, at: datetime):
        self.get_mandate(mandate_id)
        return self._audit(EVT_RESUMED, {KEY_MANDATE_ID: mandate_id, "at": at.isoformat()}, at=at)

    def revoke_mandate(self, mandate_id: str, *, at: datetime, reason: str):
        self.get_mandate(mandate_id)
        return self._audit(EVT_REVOKED, {KEY_MANDATE_ID: mandate_id, "reason": reason, "at": at.isoformat()}, at=at)

    def top_up_mandate(
        self,
        mandate_id: str,
        *,
        add_collateral: Money,
        funding_account_id: str,
        at: datetime,
    ):
        """手作りの明示時刻トランザクションで escrow に担保を追加し、有効枠を引き上げる。"""
        mandate = self.get_mandate(mandate_id)
        if add_collateral.currency != mandate.currency:
            raise MandateError("top-up currency must match mandate")
        if not add_collateral.is_positive():
            raise MandateError("top-up amount must be positive")
        b = TransactionBuilder("MANDATE_TOP_UP", mandate.principal_id, initiated_at=at)
        b.transfer(funding_account_id, mandate.escrow_account_id, add_collateral)
        tx = b.build(status=TransactionStatus.SETTLED, settled_at=at)
        self._ledger.post(tx)
        evt = self._audit(
            EVT_TOPPED_UP,
            {
                KEY_MANDATE_ID: mandate_id,
                "add_collateral_cents": add_collateral.cents,
                "funding_account_id": funding_account_id,
                "transaction_id": tx.transaction_id,
                "at": at.isoformat(),
            },
            at=at,
        )
        return tx, evt

    # ---------- 再導出（元帳から） ----------

    def _records(self, as_of: datetime) -> list[SettlementRecord]:
        return list(iter_settlement_records(self._ledger, as_of=as_of))

    def _leaf_records(self, mandate_id: str, as_of: datetime) -> list[SettlementRecord]:
        return [r for r in self._records(as_of) if r.mandate_id == mandate_id]

    def settled_total_cents(self, mandate_id: str, as_of: datetime) -> int:
        """このリーフ委任枠の累計消化（authorized_outflow, 予算平面）。"""
        return sum(r.authorized_outflow_cents for r in self._leaf_records(mandate_id, as_of))

    def subtree_settled_cents(self, mandate_id: str, as_of: datetime) -> int:
        ids = descendant_ids(self._mandates, mandate_id)
        return sum(
            r.authorized_outflow_cents for r in self._records(as_of) if r.mandate_id in ids
        )

    def effective_cap_cents(self, mandate_id: str, at: datetime) -> int:
        return self.mandate_state(mandate_id, at).effective_budget_cap_cents

    def remaining_cents(self, mandate_id: str, as_of: datetime) -> int:
        return self.effective_cap_cents(mandate_id, as_of) - self.subtree_settled_cents(
            mandate_id, as_of
        )

    def settled_intent_ids(self, mandate_id: str, as_of: datetime) -> set[str]:
        return {r.intent_id for r in self._leaf_records(mandate_id, as_of)}

    def payee_receipts_cents(self, mandate_id: str, as_of: datetime) -> dict[str, int]:
        """payee 別の実受領累計（受領平面, payee_receipt）。"""
        receipts: dict[str, int] = {}
        for r in self._leaf_records(mandate_id, as_of):
            receipts[r.payee_account_id] = receipts.get(r.payee_account_id, 0) + r.payee_receipt_cents
        return receipts

    def payee_receipt_currency(self, mandate_id: str, as_of: datetime) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self._leaf_records(mandate_id, as_of):
            out[r.payee_account_id] = r.payee_receipt_currency_code
        return out

    def escrow_balance_cents(self, mandate_id: str, as_of: datetime) -> int:
        mandate = self.get_mandate(mandate_id)
        return self._ledger.balance(mandate.escrow_account_id, as_of=as_of).cents

    def co_escrow_remaining_cents(self, mandate_id: str, as_of: datetime) -> int:
        """同一 escrow を共有する全委任枠の remaining の総和（担保の基礎）。"""
        escrow = self.get_mandate(mandate_id).escrow_account_id
        total = 0
        for mid, m in self._mandates.items():
            if m.escrow_account_id == escrow and m.parent_mandate_id is None:
                # root 単位で subtree remaining を数える（子は subtree に含まれ二重計上しない）
                total += self.remaining_cents(mid, as_of)
        return total

    def max_settled_nonce(self, mandate_id: str, as_of: datetime) -> int:
        return max(
            (r.nonce for r in self._leaf_records(mandate_id, as_of) if r.nonce is not None),
            default=-1,
        )

    def last_settled_at(self, mandate_id: str, as_of: datetime) -> datetime | None:
        times = [r.settled_at for r in self._leaf_records(mandate_id, as_of)]
        return max(times) if times else None

    def _epoch_index(self, mandate: Mandate, at: datetime) -> int | None:
        pol = mandate.spend_policy
        if pol is not None and pol.epoch is not None:
            return pol.epoch.epoch_index(at)
        return None

    # ---------- 認可プリミティブ ----------

    def _authorize(
        self,
        mandate_id: str,
        intent_id: str,
        amount: Money,
        purpose: str,
        payee_account_id: str,
        at: datetime,
        nonce: int | None = None,
        *,
        extra_records: Sequence[SettlementRecord] = (),
    ) -> tuple[bool, str, int]:
        """DENIAL_ORDER を 1 度だけ走らせる。(ok, reason, remaining_before) を返す。"""
        mandate = self.get_mandate(mandate_id)

        if amount.currency != mandate.currency:
            return False, "CURRENCY_MISMATCH", 0
        if not amount.is_positive():
            return False, "NON_POSITIVE_AMOUNT", 0

        view = self.mandate_state(mandate_id, at)
        if view.state == MandateState.REVOKED:
            return False, "MANDATE_REVOKED", 0
        if view.state == MandateState.EXPIRED:
            return False, "MANDATE_EXPIRED", 0
        if view.state == MandateState.PAUSED:
            return False, "MANDATE_PAUSED", 0

        if at < mandate.valid_from or at > mandate.valid_until:
            return False, "OUTSIDE_WINDOW", 0

        # 認可は「これまでに成立した全決済」を読む（as-of=at ではない）。そうしないと
        # `at` を過去に戻すバックデートで将来の決済が見えず、単調時刻ガードを迂回できる。
        # 単調時刻ガードが先に落とすので、以降の as-of 依存計算は正しい順序でのみ走る。
        all_records = list(self._records(_DATETIME_MAX)) + list(extra_records)
        leaf = [r for r in all_records if r.mandate_id == mandate_id]

        effective_cap = view.effective_budget_cap_cents
        subtree_ids = descendant_ids(self._mandates, mandate_id)
        subtree_settled = sum(
            r.authorized_outflow_cents for r in all_records if r.mandate_id in subtree_ids
        )
        remaining_before = effective_cap - subtree_settled

        prior_times = [r.settled_at for r in leaf]
        if prior_times and at < max(prior_times):
            return False, "NON_MONOTONIC_TIME", remaining_before

        if purpose not in mandate.allowed_purposes:
            return False, "PURPOSE_NOT_ALLOWED", remaining_before

        pol = mandate.spend_policy
        if pol is not None and pol.payee_allowlist is not None and payee_account_id not in pol.payee_allowlist:
            return False, "PAYEE_NOT_ALLOWED", remaining_before
        if pol is not None and pol.min_amount_cents is not None and amount.cents < pol.min_amount_cents:
            return False, "BELOW_MIN_AMOUNT", remaining_before
        if pol is not None and pol.max_amount_cents is not None and amount.cents > pol.max_amount_cents:
            return False, "ABOVE_MAX_AMOUNT", remaining_before

        if mandate.per_transaction_limit is not None and amount > mandate.per_transaction_limit:
            return False, "PER_TX_LIMIT_EXCEEDED", remaining_before

        if mandate.nonce_required:
            hw = max((r.nonce for r in leaf if r.nonce is not None), default=-1)
            if nonce is None:
                return False, "NONCE_NOT_INCREASING", remaining_before
            if any(r.nonce == nonce for r in leaf):
                return False, "NONCE_REUSED", remaining_before
            if nonce <= hw:
                return False, "NONCE_NOT_INCREASING", remaining_before

        if any(r.intent_id == intent_id for r in leaf):
            return False, "DUPLICATE_INTENT", remaining_before

        if pol is not None:
            sb = pol.sub_budget_for(purpose)
            if sb is not None:
                spent_purpose = sum(r.authorized_outflow_cents for r in leaf if r.purpose == purpose)
                if spent_purpose + amount.cents > sb:
                    return False, "SUB_BUDGET_EXCEEDED", remaining_before

        if pol is not None and pol.epoch is not None:
            idx = pol.epoch.epoch_index(at)
            if pol.epoch_settlement_cap is not None:
                cnt = sum(1 for r in leaf if pol.epoch.epoch_index(r.settled_at) == idx)
                if cnt + 1 > pol.epoch_settlement_cap:
                    return False, "EPOCH_VELOCITY_EXCEEDED", remaining_before

        if pol is not None and pol.rolling_window_seconds is not None:
            lo = at - timedelta(seconds=pol.rolling_window_seconds)
            if pol.rolling_window_settlement_cap is not None:
                cnt = sum(1 for r in leaf if lo <= r.settled_at <= at)
                if cnt + 1 > pol.rolling_window_settlement_cap:
                    return False, "WINDOW_VELOCITY_EXCEEDED", remaining_before

        if pol is not None and pol.epoch is not None and pol.epoch_spend_cap_cents is not None:
            idx = pol.epoch.epoch_index(at)
            spent = sum(
                r.authorized_outflow_cents for r in leaf if pol.epoch.epoch_index(r.settled_at) == idx
            )
            if spent + amount.cents > pol.epoch_spend_cap_cents:
                return False, "EPOCH_CAP_EXCEEDED", remaining_before

        if pol is not None and pol.rolling_window_seconds is not None and pol.rolling_window_spend_cap_cents is not None:
            lo = at - timedelta(seconds=pol.rolling_window_seconds)
            spent = sum(r.authorized_outflow_cents for r in leaf if lo <= r.settled_at <= at)
            if spent + amount.cents > pol.rolling_window_spend_cap_cents:
                return False, "WINDOW_CAP_EXCEEDED", remaining_before

        for anc in ancestor_ids(self._mandates, mandate_id):
            anc_cap = self.effective_cap_cents(anc, at)
            anc_ids = descendant_ids(self._mandates, anc)
            anc_settled = sum(
                r.authorized_outflow_cents for r in all_records if r.mandate_id in anc_ids
            )
            if anc_settled + amount.cents > anc_cap:
                return False, "PARENT_BUDGET_EXCEEDED", remaining_before

        if subtree_settled + amount.cents > effective_cap:
            return False, "BUDGET_EXCEEDED", remaining_before

        return True, "OK", remaining_before

    def preauthorize(
        self,
        *,
        mandate_id: str,
        intent_id: str,
        payee_account_id: str,
        amount: Money,
        purpose: str,
        at: datetime,
        nonce: int | None = None,
    ) -> tuple[bool, str, int]:
        """副作用なしの認可ドライラン。(ok, reason, remaining_before) を返す。

        x402 ファシリテーターの /verify（元帳を変えずに支払い可否を判定）に使う。
        """
        return self._authorize(mandate_id, intent_id, amount, purpose, payee_account_id, at, nonce)

    def _settlement_metadata(
        self,
        mandate: Mandate,
        *,
        intent_id: str,
        payee_account_id: str,
        nonce: int | None,
        epoch_index: int | None,
        kind: str,
        extra: Sequence[tuple[str, str]] = (),
    ) -> list[tuple[str, str]]:
        meta: list[tuple[str, str]] = [
            (KEY_TXN_TYPE, VAL_INTENT_SETTLEMENT),
            (KEY_MANDATE_ID, mandate.mandate_id),
            (KEY_ROOT_MANDATE_ID, root_id_of(self._mandates, mandate.mandate_id)),
            (KEY_MANDATE_PATH, mandate_path(self._mandates, mandate.mandate_id)),
            (KEY_INTENT_ID, intent_id),
            (KEY_PAYEE, payee_account_id),
            (KEY_ESCROW, mandate.escrow_account_id),
            (KEY_KIND, kind),
        ]
        if nonce is not None:
            meta.append((KEY_NONCE, str(nonce)))
        if epoch_index is not None:
            meta.append((KEY_EPOCH, str(epoch_index)))
        meta.extend(extra)
        return meta

    def _post_settlement(
        self,
        *,
        mandate: Mandate,
        at: datetime,
        purpose: str,
        entries: Sequence[tuple[str, Money]],
        metadata: Sequence[tuple[str, str]],
    ):
        """1 つの balanced Transaction を組み立てて SETTLED で記帳する（明示時刻）。"""
        b = TransactionBuilder(purpose, mandate.principal_id, initiated_at=at)
        for account_id, money in entries:
            b.add_entry(account_id, money)
        for k, v in metadata:
            b.with_metadata(k, v)
        tx = b.build(status=TransactionStatus.SETTLED, settled_at=at)
        self._ledger.post(tx)
        return tx

    # ---------- 単発インテント決済 ----------

    def settle_intent(
        self,
        *,
        mandate_id: str,
        intent_id: str,
        payee_account_id: str,
        amount: Money,
        purpose: str,
        at: datetime,
        nonce: int | None = None,
    ) -> IntentSettlementResult:
        mandate = self.get_mandate(mandate_id)
        ok, reason, remaining_before = self._authorize(
            mandate_id, intent_id, amount, purpose, payee_account_id, at, nonce
        )
        if not ok:
            evt = self._audit(
                "intent_denied",
                {
                    KEY_MANDATE_ID: mandate_id,
                    KEY_INTENT_ID: intent_id,
                    "payee_account_id": payee_account_id,
                    "amount_cents": amount.cents,
                    "currency": amount.currency.code,
                    "purpose": purpose,
                    "reason": reason,
                    "at": at.isoformat(),
                    "remaining_cents": remaining_before,
                },
                at=at,
            )
            return IntentSettlementResult(
                intent_id=intent_id,
                mandate_id=mandate_id,
                decision="DENIED",
                amount=amount,
                purpose=purpose,
                payee_account_id=payee_account_id,
                reason=reason,
                decided_at=at,
                remaining_after_cents=remaining_before,
                transaction_id=None,
                audit_sequence=evt.sequence if evt else None,
            )

        epoch_index = self._epoch_index(mandate, at)
        meta = self._settlement_metadata(
            mandate,
            intent_id=intent_id,
            payee_account_id=payee_account_id,
            nonce=nonce,
            epoch_index=epoch_index,
            kind=KIND_PLAIN,
        )
        tx = self._post_settlement(
            mandate=mandate,
            at=at,
            purpose=purpose,
            entries=[(mandate.escrow_account_id, -amount), (payee_account_id, amount)],
            metadata=meta,
        )
        remaining_after = remaining_before - amount.cents
        evt = self._audit(
            "intent_settled",
            {
                KEY_MANDATE_ID: mandate_id,
                KEY_INTENT_ID: intent_id,
                "payee_account_id": payee_account_id,
                "amount_cents": amount.cents,
                "currency": amount.currency.code,
                "purpose": purpose,
                "transaction_id": tx.transaction_id,
                "settled_at": at.isoformat(),
                "remaining_after_cents": remaining_after,
            },
            at=at,
        )
        return IntentSettlementResult(
            intent_id=intent_id,
            mandate_id=mandate_id,
            decision="SETTLED",
            amount=amount,
            purpose=purpose,
            payee_account_id=payee_account_id,
            reason="OK",
            decided_at=at,
            remaining_after_cents=remaining_after,
            transaction_id=tx.transaction_id,
            audit_sequence=evt.sequence if evt else None,
        )

    # ---------- バッチ決済（アトミック） ----------

    def settle_batch(
        self,
        *,
        mandate_id: str,
        intents: Sequence[IntentRequest],
        at: datetime,
    ) -> BatchSettlementResult:
        """複数インテントを 1 tx でアトミックに決済する（全件成立 or 全件却下）。"""
        mandate = self.get_mandate(mandate_id)
        if not intents:
            raise MandateError("settle_batch requires at least one intent")

        epoch_index = self._epoch_index(mandate, at)
        accepted: list[SettlementRecord] = []
        for req in intents:
            ok, reason, _rem = self._authorize(
                mandate_id,
                req.intent_id,
                req.amount,
                req.purpose,
                req.payee_account_id,
                at,
                req.nonce,
                extra_records=accepted,
            )
            if not ok:
                evt = self._audit(
                    "intent_batch_denied",
                    {
                        KEY_MANDATE_ID: mandate_id,
                        "offending_intent_id": req.intent_id,
                        "reason": reason,
                        "at": at.isoformat(),
                        "num_intents": len(intents),
                    },
                    at=at,
                )
                return BatchSettlementResult(
                    mandate_id=mandate_id,
                    decision="DENIED",
                    reason=f"{reason}@{req.intent_id}",
                    transaction_id=None,
                    per_intent=(),
                    audit_sequence=evt.sequence if evt else None,
                    remaining_after_cents=self.remaining_cents(mandate_id, at),
                )
            accepted.append(
                SettlementRecord(
                    transaction_id="",
                    intent_id=req.intent_id,
                    mandate_id=mandate_id,
                    root_mandate_id=root_id_of(self._mandates, mandate_id),
                    authorized_outflow_cents=req.amount.cents,
                    payee_account_id=req.payee_account_id,
                    payee_receipt_cents=req.amount.cents,  # plain: receipt == outflow
                    payee_receipt_currency_code=req.amount.currency.code,
                    purpose=req.purpose,
                    nonce=req.nonce,
                    settled_at=at,
                    epoch_index=epoch_index,
                )
            )

        # 全件成立 → 1 tx を組み立てる（レグごとの escrow-debit + payee credit）
        entries: list[tuple[str, Money]] = []
        legs: list[dict[str, Any]] = []
        for req in intents:
            entries.append((mandate.escrow_account_id, -req.amount))
            entries.append((req.payee_account_id, req.amount))
            legs.append(
                {
                    "intent_id": req.intent_id,
                    "purpose": req.purpose,
                    "payee_account_id": req.payee_account_id,
                    "amount_cents": req.amount.cents,
                    "payee_receipt_cents": req.amount.cents,
                    "nonce": req.nonce,
                    "epoch_index": epoch_index,
                }
            )
        meta = self._settlement_metadata(
            mandate,
            intent_id="",
            payee_account_id="",
            nonce=None,
            epoch_index=None,
            kind=KIND_PLAIN,
            extra=[(KEY_BATCH, json.dumps(legs, sort_keys=True, separators=(",", ":")))],
        )
        tx = self._post_settlement(
            mandate=mandate, at=at, purpose="INTENT_BATCH", entries=entries, metadata=meta
        )
        evt = self._audit(
            "intent_batch_settled",
            {
                KEY_MANDATE_ID: mandate_id,
                "transaction_id": tx.transaction_id,
                "num_intents": len(intents),
                "legs": legs,
                "settled_at": at.isoformat(),
            },
            at=at,
        )
        remaining_after = self.remaining_cents(mandate_id, at)
        per_intent = tuple(
            IntentSettlementResult(
                intent_id=req.intent_id,
                mandate_id=mandate_id,
                decision="SETTLED",
                amount=req.amount,
                purpose=req.purpose,
                payee_account_id=req.payee_account_id,
                reason="OK",
                decided_at=at,
                remaining_after_cents=remaining_after,
                transaction_id=tx.transaction_id,
                audit_sequence=evt.sequence if evt else None,
            )
            for req in intents
        )
        return BatchSettlementResult(
            mandate_id=mandate_id,
            decision="SETTLED",
            reason="OK",
            transaction_id=tx.transaction_id,
            per_intent=per_intent,
            audit_sequence=evt.sequence if evt else None,
            remaining_after_cents=remaining_after,
        )

    # ---------- best-execution 橋渡し（bridge を遅延 import して cycle を避ける） ----------

    def settle_via_auction(self, **kwargs):
        """インテントを最良執行 + サープラス回収で決済する（intent.bridge に委譲）。"""
        from mandatehub.intent.bridge import settle_via_auction

        return settle_via_auction(self, **kwargs)

    def settle_batch_via_auction(self, **kwargs):
        """複数インテントを最良執行でアトミック決済する（intent.bridge に委譲）。"""
        from mandatehub.intent.bridge import settle_batch_via_auction

        return settle_batch_via_auction(self, **kwargs)

    # ---------- 監査ヘルパー ----------

    def _audit(self, event_type: str, payload: dict[str, Any], *, at: datetime):
        if self._audit_log is None:
            return None
        return self._audit_log.append(event_type, payload, timestamp=at)
