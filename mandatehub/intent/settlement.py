"""
intent/settlement.py — 元帳からの構造的な決済リーダー（再導出の単一の真実）。

予算・velocity・epoch・sub-budget・nonce・重複判定はすべてここが返す
SettlementRecord を走査して計算する（別カウンタを一切持たない）。

**二つの価値平面（最重要の規律 R1）:**
- 認可（予算）平面：`authorized_outflow_cents` = escrow 口座からの流出額（= user_limit）。
  予算/velocity/epoch の再導出はこれだけを読む。best-execution でも byte-stable。
- 受領（サープラス）平面：`payee_receipt_cents` = payee 口座への実際の入金額
  （= executed_cost）。payee_receipts / 実質手数料はこれを読む。

両者は auction 下で本当に異なる量であり、混同すると「payee が user_limit を受領した」
という虚偽の受領証明になるか、予算不変条件が壊れる。構造 + fail-closed 照合で守る。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from mandatehub.core.ledger import Ledger
from mandatehub.core.types import TransactionStatus
from mandatehub.intent.errors import SettlementIntegrityError

# 元帳メタデータのキー / 値（全経路で共有）
KEY_TXN_TYPE = "transaction_type"
VAL_INTENT_SETTLEMENT = "INTENT_SETTLEMENT"
KEY_MANDATE_ID = "mandate_id"
KEY_ROOT_MANDATE_ID = "root_mandate_id"
KEY_MANDATE_PATH = "mandate_path"
KEY_INTENT_ID = "intent_id"
KEY_PAYEE = "payee_account_id"
KEY_ESCROW = "escrow_account_id"
KEY_NONCE = "nonce"
KEY_EPOCH = "epoch_index"
KEY_KIND = "settlement_kind"  # "PLAIN" | "BEST_EX"
KIND_PLAIN = "PLAIN"
KIND_BEST_EX = "BEST_EX"
KEY_SPLIT = "split"  # canonical JSON（BEST_EX）
KEY_BATCH = "batch"  # canonical JSON legs（バッチ tx）


@dataclass(frozen=True)
class SettlementRecord:
    """1 レグ（単発 or バッチの 1 件）の決済記録。両平面の量を構造的に保持する。"""

    transaction_id: str
    intent_id: str
    mandate_id: str  # このレグを帰属させるリーフ委任枠
    root_mandate_id: str  # 委任ツリーの根（親が無ければ mandate_id と同じ）
    authorized_outflow_cents: int  # 予算平面：この escrow-negative 額（= user_limit）
    payee_account_id: str
    payee_receipt_cents: int  # 受領平面：payee への実入金額（= executed_cost）
    payee_receipt_currency_code: str
    purpose: str
    nonce: int | None
    settled_at: datetime
    epoch_index: int | None


def _parse_int(meta: dict[str, str], key: str) -> int | None:
    v = meta.get(key)
    if v is None or v == "":
        return None
    return int(v)


def _single_record(tx, meta: dict[str, str]) -> SettlementRecord:
    escrow = meta.get(KEY_ESCROW)
    if not escrow:
        raise SettlementIntegrityError(f"tx {tx.transaction_id}: missing escrow_account_id tag")
    outflow = -sum(
        e.amount.cents
        for e in tx.entries
        if e.account_id == escrow and e.amount.is_negative()
    )
    if outflow <= 0:
        raise SettlementIntegrityError(
            f"tx {tx.transaction_id}: no positive escrow outflow on {escrow}"
        )
    payee = meta.get(KEY_PAYEE, "")
    credits = [e for e in tx.entries if e.account_id == payee and e.amount.is_positive()]
    if len(credits) != 1:
        raise SettlementIntegrityError(
            f"tx {tx.transaction_id}: expected exactly one positive credit to payee {payee}, "
            f"found {len(credits)}"
        )
    receipt = credits[0].amount.cents
    receipt_ccy = credits[0].amount.currency.code
    mandate_id = meta.get(KEY_MANDATE_ID, "")
    return SettlementRecord(
        transaction_id=tx.transaction_id,
        intent_id=meta.get(KEY_INTENT_ID, ""),
        mandate_id=mandate_id,
        root_mandate_id=meta.get(KEY_ROOT_MANDATE_ID, mandate_id),
        authorized_outflow_cents=outflow,
        payee_account_id=payee,
        payee_receipt_cents=receipt,
        payee_receipt_currency_code=receipt_ccy,
        purpose=tx.purpose_code,
        nonce=_parse_int(meta, KEY_NONCE),
        settled_at=tx.settled_at,
        epoch_index=_parse_int(meta, KEY_EPOCH),
    )


def _batch_records(tx, meta: dict[str, str]) -> Iterator[SettlementRecord]:
    escrow = meta.get(KEY_ESCROW)
    if not escrow:
        raise SettlementIntegrityError(f"tx {tx.transaction_id}: missing escrow_account_id tag")
    legs: list[dict[str, Any]] = json.loads(meta[KEY_BATCH])

    # (1) escrow 流出額のマルチセット照合（予算平面）
    struct_debits = sorted(
        -e.amount.cents for e in tx.entries if e.account_id == escrow and e.amount.is_negative()
    )
    leg_amounts = sorted(int(leg["amount_cents"]) for leg in legs)
    if struct_debits != leg_amounts:
        raise SettlementIntegrityError(
            f"tx {tx.transaction_id}: batch escrow-debit multiset {struct_debits} "
            f"!= leg amounts {leg_amounts}"
        )

    # (2) payee ごとの受領額照合（受領平面）
    leg_receipt_by_payee: dict[str, int] = {}
    for leg in legs:
        p = leg["payee_account_id"]
        leg_receipt_by_payee[p] = leg_receipt_by_payee.get(p, 0) + int(leg["payee_receipt_cents"])
    for payee, total in leg_receipt_by_payee.items():
        struct = sum(
            e.amount.cents for e in tx.entries if e.account_id == payee and e.amount.is_positive()
        )
        if struct != total:
            raise SettlementIntegrityError(
                f"tx {tx.transaction_id}: payee {payee} structural credit {struct} "
                f"!= leg receipts {total}"
            )

    mandate_id = meta.get(KEY_MANDATE_ID, "")
    root_id = meta.get(KEY_ROOT_MANDATE_ID, mandate_id)
    for leg in legs:
        # payee 受領の通貨は当該 payee への正のエントリから取る
        rc = next(
            (
                e.amount.currency.code
                for e in tx.entries
                if e.account_id == leg["payee_account_id"] and e.amount.is_positive()
            ),
            None,
        )
        yield SettlementRecord(
            transaction_id=tx.transaction_id,
            intent_id=leg["intent_id"],
            mandate_id=mandate_id,
            root_mandate_id=root_id,
            authorized_outflow_cents=int(leg["amount_cents"]),
            payee_account_id=leg["payee_account_id"],
            payee_receipt_cents=int(leg["payee_receipt_cents"]),
            payee_receipt_currency_code=rc or "",
            purpose=leg["purpose"],
            nonce=(int(leg["nonce"]) if leg.get("nonce") not in (None, "") else None),
            settled_at=tx.settled_at,
            epoch_index=(
                int(leg["epoch_index"]) if leg.get("epoch_index") not in (None, "") else None
            ),
        )


def iter_settlement_records(ledger: Ledger, *, as_of: datetime) -> Iterator[SettlementRecord]:
    """as_of 時点までに SETTLED な全 INTENT_SETTLEMENT を構造的に再構成する。

    単発とバッチを透過的に同じ SettlementRecord として返す。矛盾は fail-closed
    （SettlementIntegrityError）。
    """
    for tx in ledger.iter_all_transactions():
        if tx.status != TransactionStatus.SETTLED:
            continue
        if tx.settled_at is None or tx.settled_at > as_of:
            continue
        meta = dict(tx.metadata)
        if meta.get(KEY_TXN_TYPE) != VAL_INTENT_SETTLEMENT:
            continue
        if KEY_BATCH in meta:
            yield from _batch_records(tx, meta)
        else:
            yield _single_record(tx, meta)
