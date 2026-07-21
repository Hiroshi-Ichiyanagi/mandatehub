"""
intent/bridge.py — ③↔④ の橋渡し：委任枠の自律決済を「最良執行 + サープラス回収」で行う。

インテントをソルバーオークションで最良執行し、利用者の指値と実執行コストの差
（サープラス）をポリシーに従って分配する。予算/velocity/epoch の再導出は escrow の
流出レグ（= user_limit）だけを読むため、すべての委任枠不変条件は plain な
settle_intent と完全に同一に保たれる（INV-9）。分配や受領は USER/FEE/PLATFORM/
CLEARING に着地し、予算平面には一切影響しない。

Model A（正準・自己資金・airtight）: C_in == C_out。価格改善益は利用者自身の
  headroom で、外部ソースも CLEARING も不要。best-ex + surplus の両証明を発行。
Model B（クロス通貨スワップ）: C_in != C_out。venue_clearing（CLEARING, 市場ミラー）
  を相手方に、2 通貨平面を 1 つの balanced tx で決済。surplus 証明（C_out）を発行。

決済実行そのもの（オンチェーン）は範囲外。ここが証明するのは会計の正しさ
（枠内・最良執行・公正分配）であって、スワップが物理的に実現したことではない。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Sequence

from mandatehub.core.types import Money
from mandatehub.execution.accounts import ExecutionAccounts
from mandatehub.execution.auction import AuctionOutcome
from mandatehub.execution.proofs import (
    ProofOfBestExecution,
    ProofOfBestExecutionGenerator,
    ProofOfSurplusRecapture,
    ProofOfSurplusRecaptureGenerator,
    SurplusEvent,
)
from mandatehub.execution.surplus import SurplusSplitPolicy, compute_split
from mandatehub.intent.errors import MandateError
from mandatehub.intent.results import AuctionSettlementResult, BatchSettlementResult, IntentSettlementResult
from mandatehub.intent.settlement import (
    KEY_BATCH,
    KEY_SPLIT,
    KIND_BEST_EX,
    SettlementRecord,
)
from mandatehub.intent.submandate import root_id_of


def _split_entries(accounts: ExecutionAccounts, alloc, currency) -> list[tuple[str, Money]]:
    """サープラス分配の credit レグ（0 は省略）。"""
    out: list[tuple[str, Money]] = []
    if alloc.user_rebate_cents:
        out.append((accounts.user_rebate_account_id, Money(cents=alloc.user_rebate_cents, currency=currency)))
    if alloc.operator_margin_cents:
        out.append((accounts.operator_margin_account_id, Money(cents=alloc.operator_margin_cents, currency=currency)))
    if alloc.gas_cents:
        out.append((accounts.gas_account_id, Money(cents=alloc.gas_cents, currency=currency)))
    if alloc.referrer_cents:
        if accounts.referrer_account_id is None:
            raise MandateError("referrer allocation non-zero but referrer account is None")
        out.append((accounts.referrer_account_id, Money(cents=alloc.referrer_cents, currency=currency)))
    return out


def _denied_auction_result(
    engine, *, mandate_id, intent_id, amount, purpose, payee_account_id, reason, at, remaining_before
) -> AuctionSettlementResult:
    evt = engine._audit(
        "intent_denied",
        {
            "mandate_id": mandate_id,
            "intent_id": intent_id,
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
    settlement = IntentSettlementResult(
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
    return AuctionSettlementResult(
        settlement=settlement,
        executed_cost_cents=None,
        split=None,
        best_execution=None,
        surplus_recapture=None,
        reason=reason,
    )


def _feasibility(user_limit: Money, auction: AuctionOutcome, split_policy, quoted_user_out):
    """(ok, reason, executed_cost_cents, surplus_cents, out_currency, executed_out) を返す。"""
    winner = auction.winner
    if winner is None:
        return False, "NO_WINNING_BID", None, None, None, None
    c_in = user_limit.currency
    if quoted_user_out is not None and quoted_user_out.currency != c_in:
        # Model B
        c_out = quoted_user_out.currency
        executed_out = winner.quoted_out_cents
        # payee は quoted_user_out を受け取る。0 以下だと構造リーダーが正の credit を
        # 見つけられず、以降 fail-closed になる。非正の執行は却下する。
        if quoted_user_out.cents <= 0:
            return False, "NON_POSITIVE_EXECUTION", winner.fill_cost_cents, None, c_out, executed_out
        if executed_out < quoted_user_out.cents:
            return False, "EXECUTION_ABOVE_LIMIT", winner.fill_cost_cents, None, c_out, executed_out
        surplus = executed_out - quoted_user_out.cents
        if surplus < split_policy.gas_reimbursement_cents:
            return False, "GAS_EXCEEDS_SURPLUS", winner.fill_cost_cents, surplus, c_out, executed_out
        return True, "OK", winner.fill_cost_cents, surplus, c_out, executed_out
    # Model A
    executed_cost = winner.fill_cost_cents
    # payee は executed_cost を受け取る。0 以下だと構造リーダーが正の credit を
    # 見つけられず、以降 fail-closed になる。非正の執行は却下する。
    if executed_cost <= 0:
        return False, "NON_POSITIVE_EXECUTION", executed_cost, None, c_in, None
    if executed_cost > user_limit.cents:
        return False, "EXECUTION_ABOVE_LIMIT", executed_cost, None, c_in, None
    surplus = user_limit.cents - executed_cost
    if surplus < split_policy.gas_reimbursement_cents:
        return False, "GAS_EXCEEDS_SURPLUS", executed_cost, surplus, c_in, None
    return True, "OK", executed_cost, surplus, c_in, None


def settle_via_auction(
    engine,
    *,
    mandate_id: str,
    intent_id: str,
    user_limit: Money,
    purpose: str,
    at: datetime,
    auction: AuctionOutcome,
    split_policy: SurplusSplitPolicy,
    accounts: ExecutionAccounts,
    quoted_user_out: Money | None = None,
    nonce: int | None = None,
) -> AuctionSettlementResult:
    """インテントを最良執行し、サープラスを分配して 1 tx でアトミックに決済する。"""
    mandate = engine.get_mandate(mandate_id)
    c_in = user_limit.currency

    # 1. 委任枠の認可（amount = user_limit）。失敗は mandate 却下（precedence）。
    ok, reason, remaining_before = engine._authorize(
        mandate_id, intent_id, user_limit, purpose, accounts.payee_account_id, at, nonce
    )
    if not ok:
        return _denied_auction_result(
            engine, mandate_id=mandate_id, intent_id=intent_id, amount=user_limit,
            purpose=purpose, payee_account_id=accounts.payee_account_id, reason=reason,
            at=at, remaining_before=remaining_before,
        )

    # 2. 執行可能性
    feas_ok, freason, executed_cost, surplus, c_out, executed_out = _feasibility(
        user_limit, auction, split_policy, quoted_user_out
    )
    if not feas_ok:
        return _denied_auction_result(
            engine, mandate_id=mandate_id, intent_id=intent_id, amount=user_limit,
            purpose=purpose, payee_account_id=accounts.payee_account_id, reason=freason,
            at=at, remaining_before=remaining_before,
        )

    model_b = c_out != c_in
    if model_b and (
        accounts.venue_clearing_account_id is None
        or accounts.venue_clearing_out_account_id is None
    ):
        raise MandateError(
            "Model B (cross-currency) requires venue_clearing_account_id (C_in) "
            "and venue_clearing_out_account_id (C_out)"
        )

    # payee は分配先口座と別でなければならない（同一だと payee に複数の正 credit が
    # 付き、構造リーダーが len != 1 で fail-closed になる）。
    split_accts = {
        accounts.user_rebate_account_id,
        accounts.operator_margin_account_id,
        accounts.gas_account_id,
    }
    if accounts.referrer_account_id is not None:
        split_accts.add(accounts.referrer_account_id)
    if accounts.payee_account_id in split_accts:
        raise MandateError("payee_account_id must be distinct from split/rebate accounts")

    # 3. 分配
    alloc = compute_split(surplus, split_policy)

    # 3.5 storage 層の原子的 claim（settle_intent と同じ多重ワーカー防衛線）。全ての
    # 否認チェックの後・記帳の直前に置く：正当な否認（NO_WINNING_BID 等）で intent を
    # 焼かず、read-check をすり抜けた並行リプレイだけを一意制約で DUPLICATE_INTENT にする。
    if not engine._ledger.try_claim(f"settle:{mandate_id}:{intent_id}", at=at):
        return _denied_auction_result(
            engine, mandate_id=mandate_id, intent_id=intent_id, amount=user_limit,
            purpose=purpose, payee_account_id=accounts.payee_account_id,
            reason="DUPLICATE_INTENT", at=at, remaining_before=remaining_before,
        )

    # 4. 1 つの balanced tx を組む
    epoch_index = engine._epoch_index(mandate, at)
    if not model_b:
        entries: list[tuple[str, Money]] = [
            (mandate.escrow_account_id, -user_limit),
            (accounts.payee_account_id, Money(cents=executed_cost, currency=c_in)),
        ]
        entries.extend(_split_entries(accounts, alloc, c_in))
    else:
        entries = [
            # C_in 平面（venue in ミラーが受け取る）
            (mandate.escrow_account_id, -user_limit),
            (accounts.venue_clearing_account_id, Money(cents=user_limit.cents, currency=c_in)),
            # C_out 平面（venue out ミラーが引き渡す。負残高になり得るが legitimate）
            (accounts.venue_clearing_out_account_id, Money(cents=-executed_out, currency=c_out)),
            (accounts.payee_account_id, Money(cents=quoted_user_out.cents, currency=c_out)),
        ]
        entries.extend(_split_entries(accounts, alloc, c_out))

    meta = engine._settlement_metadata(
        mandate,
        intent_id=intent_id,
        payee_account_id=accounts.payee_account_id,
        nonce=nonce,
        epoch_index=epoch_index,
        kind=KIND_BEST_EX,
        extra=[
            (
                KEY_SPLIT,
                json.dumps(
                    {
                        "surplus": alloc.surplus_cents,
                        "gas": alloc.gas_cents,
                        "user_rebate": alloc.user_rebate_cents,
                        "operator_margin": alloc.operator_margin_cents,
                        "referrer": alloc.referrer_cents,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        ],
    )
    tx = engine._post_settlement(
        mandate=mandate, at=at, purpose=purpose, entries=entries, metadata=meta
    )

    remaining_after = remaining_before - user_limit.cents

    # 5. 監査イベント（intent_settled は plain と同形状）
    engine._audit(
        "intent_settled",
        {
            "mandate_id": mandate_id,
            "intent_id": intent_id,
            "payee_account_id": accounts.payee_account_id,
            "amount_cents": user_limit.cents,
            "currency": c_in.code,
            "purpose": purpose,
            "transaction_id": tx.transaction_id,
            "settled_at": at.isoformat(),
            "remaining_after_cents": remaining_after,
        },
        at=at,
    )
    engine._audit(
        "best_execution",
        {
            "mandate_id": mandate_id,
            "intent_id": intent_id,
            "winner_id": auction.winner.solver_id,
            "executed_cost_cents": executed_cost,
            "user_limit_cents": user_limit.cents,
            "surplus_cents": surplus,
            "at": at.isoformat(),
        },
        at=at,
    )
    engine._audit(
        "surplus_recaptured",
        {
            "mandate_id": mandate_id,
            "intent_id": intent_id,
            "surplus_cents": surplus,
            "user_rebate_cents": alloc.user_rebate_cents,
            "operator_margin_cents": alloc.operator_margin_cents,
            "gas_cents": alloc.gas_cents,
            "referrer_cents": alloc.referrer_cents,
            "currency": c_out.code,
            "at": at.isoformat(),
        },
        at=at,
    )

    settlement = IntentSettlementResult(
        intent_id=intent_id,
        mandate_id=mandate_id,
        decision="SETTLED",
        amount=user_limit,
        purpose=purpose,
        payee_account_id=accounts.payee_account_id,
        reason="OK",
        decided_at=at,
        remaining_after_cents=remaining_after,
        transaction_id=tx.transaction_id,
        audit_sequence=None,
    )

    # 6. 証明（Model A は best-ex + surplus、Model B は surplus）
    best_ex: ProofOfBestExecution | None = None
    if not model_b:
        best_ex, _bt = ProofOfBestExecutionGenerator(engine.audit_log).generate(
            intent_id=intent_id,
            auction=auction,
            executed_cost_cents=executed_cost,
            user_limit_cents=user_limit.cents,
            in_currency_code=c_in.code,
            out_currency_code=c_out.code,
            split_policy=split_policy,
            posted_allocation=alloc,
            surplus_cents=surplus,
            snapshot_at=at,
            mandate_id=mandate_id,
        )
    surplus_proof, _st = ProofOfSurplusRecaptureGenerator(engine.audit_log).generate(
        surplus_events=[SurplusEvent(event_id=intent_id, surplus_cents=surplus, allocation=alloc)],
        snapshot_at=at,
        currency=c_out,
    )

    return AuctionSettlementResult(
        settlement=settlement,
        executed_cost_cents=executed_cost,
        split=alloc,
        best_execution=best_ex,
        surplus_recapture=surplus_proof,
        reason="OK",
    )


def settle_batch_via_auction(
    engine,
    *,
    mandate_id: str,
    legs: Sequence[tuple],  # (IntentRequest, AuctionOutcome, quoted_user_out|None)
    split_policy: SurplusSplitPolicy,
    accounts: ExecutionAccounts,
    at: datetime,
) -> tuple[BatchSettlementResult, tuple[ProofOfBestExecution, ...], ProofOfSurplusRecapture]:
    """複数インテントを最良執行で 1 tx にアトミック決済する（Model A）。"""
    mandate = engine.get_mandate(mandate_id)
    if not legs:
        raise MandateError("settle_batch_via_auction requires at least one leg")

    epoch_index = engine._epoch_index(mandate, at)
    accepted: list[SettlementRecord] = []
    plans: list[dict] = []

    for req, auction, quoted_user_out in legs:
        ok, reason, _rem = engine._authorize(
            mandate_id, req.intent_id, req.amount, req.purpose, req.payee_account_id, at, req.nonce,
            extra_records=accepted,
        )
        # バッチ最良執行は Model A（C_in == C_out）のみ。クロス通貨レグは fail-closed で
        # 却下する（Model-A 前提で posting すると不均衡 tx になるため）。
        if ok and quoted_user_out is not None and quoted_user_out.currency != req.amount.currency:
            ok, reason = False, "CROSS_CURRENCY_NOT_SUPPORTED_IN_BATCH"
        if ok:
            feas_ok, freason, executed_cost, surplus, c_out, _eo = _feasibility(
                req.amount, auction, split_policy, quoted_user_out
            )
            if not feas_ok:
                ok, reason = False, freason
        if not ok:
            evt = engine._audit(
                "intent_batch_denied",
                {"mandate_id": mandate_id, "offending_intent_id": req.intent_id, "reason": reason, "at": at.isoformat()},
                at=at,
            )
            denied = BatchSettlementResult(
                mandate_id=mandate_id, decision="DENIED", reason=f"{reason}@{req.intent_id}",
                transaction_id=None, per_intent=(), audit_sequence=evt.sequence if evt else None,
                remaining_after_cents=engine.remaining_cents(mandate_id, at),
            )
            empty_surplus, _ = ProofOfSurplusRecaptureGenerator(engine.audit_log).generate(
                surplus_events=[], snapshot_at=at, currency=req.amount.currency
            )
            return denied, (), empty_surplus

        alloc = compute_split(surplus, split_policy)
        plans.append(
            {"req": req, "auction": auction, "executed_cost": executed_cost, "surplus": surplus, "alloc": alloc}
        )
        accepted.append(
            SettlementRecord(
                transaction_id="", intent_id=req.intent_id, mandate_id=mandate_id,
                root_mandate_id=root_id_of(engine.mandates, mandate_id),
                authorized_outflow_cents=req.amount.cents, payee_account_id=req.payee_account_id,
                payee_receipt_cents=executed_cost, payee_receipt_currency_code=req.amount.currency.code,
                purpose=req.purpose, nonce=req.nonce, settled_at=at, epoch_index=epoch_index,
            )
        )

    # 全件成立 → 1 tx
    c = mandate.currency
    entries: list[tuple[str, Money]] = []
    legs_meta: list[dict] = []
    for p in plans:
        req = p["req"]
        entries.append((mandate.escrow_account_id, -req.amount))
        entries.append((req.payee_account_id, Money(cents=p["executed_cost"], currency=c)))
        entries.extend(_split_entries(accounts, p["alloc"], c))
        legs_meta.append(
            {
                "intent_id": req.intent_id, "purpose": req.purpose,
                "payee_account_id": req.payee_account_id, "amount_cents": req.amount.cents,
                "payee_receipt_cents": p["executed_cost"], "nonce": req.nonce, "epoch_index": epoch_index,
            }
        )
    meta = engine._settlement_metadata(
        mandate, intent_id="", payee_account_id="", nonce=None, epoch_index=None, kind=KIND_BEST_EX,
        extra=[(KEY_BATCH, json.dumps(legs_meta, sort_keys=True, separators=(",", ":")))],
    )
    tx = engine._post_settlement(mandate=mandate, at=at, purpose="INTENT_BATCH", entries=entries, metadata=meta)

    engine._audit(
        "intent_batch_settled",
        {"mandate_id": mandate_id, "transaction_id": tx.transaction_id, "num_intents": len(plans), "legs": legs_meta, "settled_at": at.isoformat()},
        at=at,
    )

    remaining_after = engine.remaining_cents(mandate_id, at)
    per_intent = []
    bestex_proofs: list[ProofOfBestExecution] = []
    surplus_events: list[SurplusEvent] = []
    for p in plans:
        req = p["req"]
        per_intent.append(
            IntentSettlementResult(
                intent_id=req.intent_id, mandate_id=mandate_id, decision="SETTLED", amount=req.amount,
                purpose=req.purpose, payee_account_id=req.payee_account_id, reason="OK", decided_at=at,
                remaining_after_cents=remaining_after, transaction_id=tx.transaction_id, audit_sequence=None,
            )
        )
        bp, _ = ProofOfBestExecutionGenerator(engine.audit_log).generate(
            intent_id=req.intent_id, auction=p["auction"], executed_cost_cents=p["executed_cost"],
            user_limit_cents=req.amount.cents, in_currency_code=c.code, out_currency_code=c.code,
            split_policy=split_policy, posted_allocation=p["alloc"], surplus_cents=p["surplus"],
            snapshot_at=at, mandate_id=mandate_id,
        )
        bestex_proofs.append(bp)
        surplus_events.append(SurplusEvent(event_id=req.intent_id, surplus_cents=p["surplus"], allocation=p["alloc"]))

    surplus_proof, _ = ProofOfSurplusRecaptureGenerator(engine.audit_log).generate(
        surplus_events=surplus_events, snapshot_at=at, currency=c
    )
    result = BatchSettlementResult(
        mandate_id=mandate_id, decision="SETTLED", reason="OK", transaction_id=tx.transaction_id,
        per_intent=tuple(per_intent), audit_sequence=None, remaining_after_cents=remaining_after,
    )
    return result, tuple(bestex_proofs), surplus_proof
