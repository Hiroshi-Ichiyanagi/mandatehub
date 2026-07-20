"""
x402/best_exec.py — `best-exec` x402 スキーム（③をスキーム化：最良執行 + サープラス回収）。

resource server が上限価格を提示し、facilitator が枠内で最良執行して差額（サープラス）を
ポリシー通りに分配・返金する。会計と証明（best-of-disclosed + 整数厳密分配）を
**オフライン検証可能**に保つ層で、③の run_auction / compute_split / settle_via_auction /
ProofOfBestExecution / ProofOfSurplusRecapture を **そのまま再利用**する（実行/証明ロジックは
一切複製しない）。

**正直な境界（設計スペック §8 準拠）:**
- ここが証明するのは *会計*（枠内・開示集合内で最良・整数厳密分配・自己整合な証明）であって、
  資金がオンチェーンで動いたことではない。「資金が動いた」は tx を読む **オンライン1手**（§checkの step 9）。
- オンチェーンの安全性（原子的分配・返金withholding不能・nonce拘束）は監査済みの
  `BestExecSettler` コントラクト（**未構築・コア外・要監査**）に依存する。ここでは
  in-ledger の合成レシート（InLedgerSettlementAdapter）で会計を検証するのみ。
- keccak256 は stdlib に無いため、**オフラインの binding/policy ハッシュは sha256**。
  オンチェーンの nonce は keccak256（2つのハッシュ領域）。offline テストは *ロジック* を
  検証し、literal な on-chain nonce は検証しない。
- 実 EIP-712/secp256k1 の署名検証はコア外（StubVerifier は決定的・鍵レス）。
- 保証は `user_no_worse_than_best_disclosed`（開示集合内で最良、市場最良ではない）と
  `user_effective_fee_vs_limit_non_positive`（利用者自身の指値対比 ≤ 0）。抑圧・一律過少報告は
  ライブオラクル無しにオフライン検出不可。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from mandatehub.core.types import Currency, Money
from mandatehub.execution.auction import OBJ_MAX_OUT, OBJ_MIN_COST, SolverBid, run_auction
from mandatehub.execution.proofs import _merkle_over  # 同一プロジェクト内の再利用
from mandatehub.execution.surplus import SurplusSplitPolicy, compute_split
from mandatehub.intent.bridge import _feasibility, settle_via_auction
from mandatehub.x402.wire import EIP3009Authorization

SCHEME_BEST_EXEC = "best-exec"
DEFAULT_PURPOSE = "X402_BEST_EXEC"


# ---------- ポリシー / binding のシリアライズ + ハッシュ（オフライン = sha256） ----------


def split_policy_to_wire(p: SurplusSplitPolicy) -> dict[str, int]:
    return {
        "user_rebate_bps": p.user_rebate_bps,
        "operator_margin_bps": p.operator_margin_bps,
        "referrer_bps": p.referrer_bps,
        "gas_reimbursement_cents": p.gas_reimbursement_cents,
    }


def split_policy_from_wire(d: dict[str, Any]) -> SurplusSplitPolicy:
    return SurplusSplitPolicy(
        user_rebate_bps=int(d["user_rebate_bps"]),
        operator_margin_bps=int(d["operator_margin_bps"]),
        referrer_bps=int(d.get("referrer_bps", 0)),
        gas_reimbursement_cents=int(d.get("gas_reimbursement_cents", 0)),
    )


def _sha256_hex(obj: Any) -> str:
    return "0x" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def split_policy_hash(p: SurplusSplitPolicy) -> str:
    """オフラインの分配ポリシーハッシュ（sha256; on-chain は keccak256 — 別領域）。"""
    return _sha256_hex(split_policy_to_wire(p))


def binding_preimage(
    *,
    chain_id: int,
    settler: str,
    asset: str,
    max_amount: str,
    pay_to: str,
    rebate_to: str,
    operator_to: str,
    split_policy_hash_hex: str,
    objective: str,
    intent_id: str,
    valid_after: str,
    valid_before: str,
) -> dict[str, Any]:
    return {
        "chainId": chain_id,
        "settler": settler,
        "asset": asset,
        "maxAmount": max_amount,
        "payTo": pay_to,
        "rebateTo": rebate_to,
        "operatorTo": operator_to,
        "splitPolicyHash": split_policy_hash_hex,
        "objective": objective,
        "intentId": intent_id,
        "validAfter": valid_after,
        "validBefore": valid_before,
    }


def binding_digest(preimage: dict[str, Any]) -> str:
    """binding の消化不能コミットメント（オフライン sha256; on-chain は keccak256）。"""
    return _sha256_hex(preimage)


# ---------- best-exec の拡張パラメータ + ペイロード ----------


@dataclass(frozen=True)
class BestExecParams:
    """PaymentRequirements.extra["bestExec"] の中身。"""

    objective: str  # "MIN_COST" | "MAX_OUT"
    settler: str  # EIP-3009 の `to`（監査済み BestExecSettler コントラクト）
    split_policy: SurplusSplitPolicy
    rebate_to: str
    operator_to: str
    currency_in: str = "USDC"
    currency_out: str = "USDC"
    settler_code_hash: str = ""
    mandate_ref: str | None = None
    purpose: str = DEFAULT_PURPOSE
    fallback_scheme: str = "exact"

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": 1,
            "objective": self.objective,
            "settler": self.settler,
            "settlerCodeHash": self.settler_code_hash,
            "splitPolicy": split_policy_to_wire(self.split_policy),
            "rebateTo": self.rebate_to,
            "operatorTo": self.operator_to,
            "currency": {"in": self.currency_in, "out": self.currency_out},
            "mandateRef": self.mandate_ref,
            "purpose": self.purpose,
            "fallbackScheme": self.fallback_scheme,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "BestExecParams":
        cur = d.get("currency") or {}
        return cls(
            objective=d["objective"],
            settler=d["settler"],
            split_policy=split_policy_from_wire(d["splitPolicy"]),
            rebate_to=d["rebateTo"],
            operator_to=d["operatorTo"],
            currency_in=cur.get("in", "USDC"),
            currency_out=cur.get("out", "USDC"),
            settler_code_hash=d.get("settlerCodeHash", ""),
            mandate_ref=d.get("mandateRef"),
            purpose=d.get("purpose", DEFAULT_PURPOSE),
            fallback_scheme=d.get("fallbackScheme", "exact"),
        )


@dataclass(frozen=True)
class BestExecPayload:
    """best-exec の PaymentPayload（authorization + binding preimage）。"""

    authorization: EIP3009Authorization
    signature: str | None
    binding: dict[str, Any]
    network: str = "base-sepolia"

    def to_wire(self) -> dict[str, Any]:
        return {
            "x402Version": 1,
            "scheme": SCHEME_BEST_EXEC,
            "network": self.network,
            "payload": {
                "signature": self.signature,
                "authorization": self.authorization.to_wire(),
                "bestExecBinding": self.binding,
            },
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "BestExecPayload":
        p = d["payload"]
        return cls(
            authorization=EIP3009Authorization.from_wire(p["authorization"]),
            signature=p.get("signature"),
            binding=p["bestExecBinding"],
            network=d.get("network", "base-sepolia"),
        )


# ---------- Signer / Verifier / SettlementAdapter（すべて stub / offline） ----------


class Verifier(Protocol):
    def recover(self, authorization: EIP3009Authorization, signature: str | None) -> str: ...


class StubVerifier:
    """決定的・鍵レス。authorization.from をそのまま返す（実 secp256k1 復元はコア外）。"""

    def recover(self, authorization: EIP3009Authorization, signature: str | None) -> str:
        return authorization.from_


class SettlementAdapter(Protocol):
    def execute(self, plan: dict[str, Any]) -> dict[str, Any]: ...


class InLedgerSettlementAdapter:
    """既定：オンチェーンには出ず、元帳 tx から合成レシートを返す（完全オフライン）。

    実 on-chain 版（OnChainAdapter）は別パッケージ（web3 + keccak + 鍵）で、コアは import しない。
    """

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "plane": "in-ledger",  # 合成レシート — 資金はオンチェーンで動いていない（正直な境界）
            "txHash": "0xLEDGER:" + plan["ledger_tx"],  # 合成（オンチェーンではない）
            "settler": plan["settler"],
            "settlerCodeHash": plan.get("settler_code_hash", ""),
            "rebateLeg": {"to": plan["rebate_to"], "value": str(plan["user_rebate"])},
            "payToLeg": {"to": plan["pay_to"], "value": str(plan["executed_cost"])},
        }


def build_best_exec_payload(
    requirements,
    *,
    from_addr: str,
    at: datetime,
    intent_id: str,
    chain_id: int = 84532,
    network: str = "base-sepolia",
    ttl: int = 60,
    skew: int = 60,
    signature: str | None = None,
) -> BestExecPayload:
    """要件から binding をコミットした best-exec ペイロードを組む（テスト/クライアント用）。

    値は max（固定）。nonce = binding_digest(preimage)（オフライン sha256）。
    """
    be = BestExecParams.from_wire((requirements.extra or {})["bestExec"])
    now = int(at.timestamp())
    valid_after, valid_before = str(now - skew), str(now + ttl)
    pre = binding_preimage(
        chain_id=chain_id,
        settler=be.settler,
        asset=requirements.asset,
        max_amount=requirements.max_amount_required,
        pay_to=requirements.pay_to,
        rebate_to=be.rebate_to,
        operator_to=be.operator_to,
        split_policy_hash_hex=split_policy_hash(be.split_policy),
        objective=be.objective,
        intent_id=intent_id,
        valid_after=valid_after,
        valid_before=valid_before,
    )
    auth = EIP3009Authorization(
        from_=from_addr,
        to=be.settler,
        value=requirements.max_amount_required,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=binding_digest(pre),
    )
    return BestExecPayload(
        authorization=auth, signature=signature or ("0x" + "be" * 65), binding=pre, network=network
    )


# ---------- best-exec ファシリテーター ----------


@dataclass(frozen=True)
class BestExecResult:
    success: bool
    reason: str
    response: dict[str, Any] | None = field(default=None)


class BestExecFacilitator:
    """1つの mandate + ExecutionAccounts に束ねた best-exec スキームハンドラ。

    verify/settle は wire アダプタに徹し、run_auction / _feasibility / settle_via_auction を
    そのまま呼ぶ。bids（開示された SolverBid 群）は facilitator が場外で集めて渡す。
    """

    def __init__(
        self,
        engine,
        mandate_id: str,
        accounts,
        *,
        network: str = "base-sepolia",
        chain_id: int = 84532,
        verifier: Verifier | None = None,
        adapter: SettlementAdapter | None = None,
    ) -> None:
        self._engine = engine
        self._mandate_id = mandate_id
        self._accounts = accounts
        self._network = network
        self._chain_id = chain_id
        self._verifier = verifier or StubVerifier()
        self._adapter = adapter or InLedgerSettlementAdapter()

    def _currency(self, code: str) -> Currency:
        for c in Currency:
            if c.code == code:
                return c
        raise ValueError(f"unknown asset currency: {code}")

    def verify(
        self,
        requirements,  # X402PaymentRequirements
        payload: BestExecPayload,
        bids: Sequence[SolverBid],
        *,
        at: datetime,
        on_chain: bool = False,
    ) -> tuple[bool, str]:
        be = BestExecParams.from_wire((requirements.extra or {})["bestExec"])
        auth = payload.authorization
        max_amount = requirements.max_amount_required

        if auth.value != max_amount:
            return False, "VALUE_NOT_MAX"
        if not (int(auth.valid_after) <= int(at.timestamp()) <= int(auth.valid_before)):
            return False, "OUTSIDE_WINDOW"

        # nonce-binding：binding preimage の digest が署名済み nonce と一致すること。
        # digest はプリイメージ全体の sha256 なので、いずれかの束縛フィールドを弄れば
        # digest が変わり nonce と不一致になる。加えて、attacker が nonce を再計算して
        # 自己整合な binding を作った場合に備え、**全ての束縛フィールド**を requirements /
        # authorization / be / facilitator 設定と突き合わせる（部分照合では素通りする）。
        if binding_digest(payload.binding) != auth.nonce:
            return False, "POLICY_MISMATCH"
        bnd = payload.binding
        if (
            bnd.get("settler") != be.settler
            or bnd.get("asset") != requirements.asset
            or str(bnd.get("maxAmount")) != max_amount
            or bnd.get("payTo") != requirements.pay_to
            or bnd.get("rebateTo") != be.rebate_to
            or bnd.get("operatorTo") != be.operator_to
            or bnd.get("splitPolicyHash") != split_policy_hash(be.split_policy)
            or bnd.get("objective") != be.objective
            or int(bnd.get("chainId", -1)) != self._chain_id
            or str(bnd.get("validAfter")) != auth.valid_after
            or str(bnd.get("validBefore")) != auth.valid_before
            or not bnd.get("intentId")
        ):
            return False, "POLICY_MISMATCH"

        # funding-plane（§5.3）：この in-ledger ファシリテーターは必ず mandate 面で決済する
        # （on-chain 面は OnChainAdapter が要り、コア外）。mandate_ref 必須。
        if be.mandate_ref is None:
            return False, "MANDATE_REF_REQUIRED"
        if be.mandate_ref != self._mandate_id:
            return False, "MANDATE_REF_MISMATCH"

        if on_chain:
            if be.currency_in != be.currency_out:
                return False, "CROSS_CURRENCY_NOT_SUPPORTED"
            if be.objective == OBJ_MAX_OUT:
                return False, "MAX_OUT_NOT_SUPPORTED_ONCHAIN"

        currency = self._currency(be.currency_in)
        amount = Money(cents=int(max_amount), currency=currency)
        intent_id = payload.binding["intentId"]

        # funding-plane：mandateRef ありは in-ledger（escrow 引き落としが pull を模す）
        if be.mandate_ref is not None:
            ok, reason, _rem = self._engine.preauthorize(
                mandate_id=self._mandate_id,
                intent_id=intent_id,
                payee_account_id=self._accounts.payee_account_id,
                amount=amount,
                purpose=be.purpose,
                at=at,
                nonce=None,
            )
            if not ok:
                return False, reason

        auction = run_auction(list(bids), objective=be.objective)
        feas_ok, freason, *_rest = _feasibility(amount, auction, be.split_policy, None)
        if not feas_ok:
            return False, freason
        return True, "OK"

    def settle(
        self,
        requirements,
        payload: BestExecPayload,
        bids: Sequence[SolverBid],
        *,
        at: datetime,
    ) -> BestExecResult:
        ok, reason = self.verify(requirements, payload, bids, at=at)
        if not ok:
            return BestExecResult(success=False, reason=reason)

        be = BestExecParams.from_wire((requirements.extra or {})["bestExec"])
        currency = self._currency(be.currency_in)
        amount = Money(cents=int(requirements.max_amount_required), currency=currency)
        intent_id = payload.binding["intentId"]
        auction = run_auction(list(bids), objective=be.objective)

        result = settle_via_auction(
            self._engine,
            mandate_id=self._mandate_id,
            intent_id=intent_id,
            user_limit=amount,
            purpose=be.purpose,
            at=at,
            auction=auction,
            split_policy=be.split_policy,
            accounts=self._accounts,
        )
        if not result.settlement.is_settled:
            return BestExecResult(success=False, reason=result.reason)

        alloc = result.split
        be_proof = result.best_execution
        sr_proof = result.surplus_recapture
        receipt = self._adapter.execute(
            {
                "ledger_tx": result.settlement.transaction_id,
                "settler": be.settler,
                "settler_code_hash": be.settler_code_hash,
                "pay_to": requirements.pay_to,
                "rebate_to": be.rebate_to,
                "executed_cost": result.executed_cost_cents,
                "user_rebate": alloc.user_rebate_cents,
            }
        )
        response = {
            "success": True,
            "scheme": SCHEME_BEST_EXEC,
            "network": self._network,
            # 正直な境界：この facilitator の決済面。in-ledger は合成レシート（オンチェーンで
            # 資金が動いたことは *証明しない*）。OnChainAdapter を差せば "on-chain" になる。
            "settlementPlane": receipt.get("plane", "in-ledger"),
            "txHash": receipt["txHash"],
            "settler": be.settler,
            "settlerCodeHash": be.settler_code_hash,
            "executedCost": str(result.executed_cost_cents),
            "maxAmount": requirements.max_amount_required,
            # nonce-binding のコミットメント（公開情報）。第三者が binding_digest(binding)==nonce と
            # binding の各フィールド（policyHash/objective/maxAmount 等）をオフライン照合できる。
            "binding": payload.binding,
            "nonce": payload.authorization.nonce,
            "rebateLeg": receipt["rebateLeg"],
            "payToLeg": receipt["payToLeg"],
            "split": {
                "surplus": str(alloc.surplus_cents),
                "gas": str(alloc.gas_cents),
                "user_rebate": str(alloc.user_rebate_cents),
                "operator_margin": str(alloc.operator_margin_cents),
                "referrer": str(alloc.referrer_cents),
            },
            "auction": {
                "objective": be.objective,
                "winnerId": auction.winner.solver_id if auction.winner else "",
                "referenceCost": (str(auction.reference.fill_cost_cents) if auction.reference else None),
                "candidates": [
                    {
                        "solver_id": b.solver_id,
                        "fill_cost_cents": b.fill_cost_cents,
                        "quoted_out_cents": b.quoted_out_cents,
                        "gas_cents": b.gas_cents,
                        "valid": b.valid,
                    }
                    for b in ([auction.winner] if auction.winner else []) + list(auction.losers) + list(auction.invalid)
                ],
                "candidatesMerkleRoot": be_proof.candidates_root if be_proof else "",
            },
            "proofOfBestExecution": {**be_proof.to_public_summary(), "artifactHash": be_proof.artifact_hash()} if be_proof else None,
            "proofOfSurplusRecapture": {**sr_proof.to_public_summary(), "artifactHash": sr_proof.artifact_hash()},
        }
        return BestExecResult(success=True, reason="OK", response=response)


# ---------- オフライン検証（第三者が response だけから再計算：§4 step 1-8） ----------


def verify_best_exec_response(
    response: dict[str, Any],
    *,
    agreed_policy: SurplusSplitPolicy,
    in_currency_code: str = "USDC",
) -> dict[str, bool]:
    """response 単体から会計を再検証する（オフライン、stdlib のみ）。step 9=オンチェーン確認は含まない。"""
    auc = response["auction"]
    objective = auc["objective"]
    bids = [
        SolverBid(
            solver_id=c["solver_id"],
            intent_id="",
            fill_cost_cents=int(c["fill_cost_cents"]),
            quoted_out_cents=int(c["quoted_out_cents"]),
            gas_cents=int(c["gas_cents"]),
            valid=bool(c.get("valid", True)),
        )
        for c in auc["candidates"]
    ]
    outcome = run_auction(bids, objective=objective)
    winner_ok = outcome.winner is not None and outcome.winner.solver_id == auc["winnerId"]

    executed_cost = int(response["executedCost"])
    max_amount = int(response["maxAmount"])
    if objective == OBJ_MIN_COST:
        cost_ok = outcome.winner is not None and executed_cost == outcome.winner.fill_cost_cents
    else:
        cost_ok = True  # MAX_OUT: executed_cost はコスト軸では拘束されない

    # 独立な「開示集合内で最良か」の再計算。証明が自己申告する
    # user_no_worse_than_best_disclosed フラグを *信用せず*、候補から直接算出する
    # （悪意の facilitator がフラグだけ True にしても、この照合は素通りしない）。
    valid_bids = [b for b in bids if b.valid]
    if objective == OBJ_MIN_COST:
        best_disclosed = min((b.fill_cost_cents for b in valid_bids), default=None)
        independent_no_worse = best_disclosed is not None and executed_cost <= best_disclosed
    else:
        best_out = max((b.quoted_out_cents for b in valid_bids), default=None)
        independent_no_worse = (
            outcome.winner is not None
            and best_out is not None
            and outcome.winner.quoted_out_cents == best_out
        )

    # 候補コミットメント再構築
    disclosed = {b.solver_id: b.fill_cost_cents for b in bids}
    root = _merkle_over(disclosed, in_currency_code, "bid:").root_hash
    root_ok = root == auc["candidatesMerkleRoot"]

    within_limit = executed_cost <= max_amount

    # 分配の再計算（合意ポリシーで）
    surplus = int(response["split"]["surplus"])
    recomputed = compute_split(surplus, agreed_policy)
    split = response["split"]
    split_ok = (
        recomputed.gas_cents == int(split["gas"])
        and recomputed.user_rebate_cents == int(split["user_rebate"])
        and recomputed.operator_margin_cents == int(split["operator_margin"])
        and recomputed.referrer_cents == int(split["referrer"])
        and recomputed.total() == surplus
    )

    # 証明側のフラグとの一致
    be = response.get("proofOfBestExecution") or {}
    sr = response.get("proofOfSurplusRecapture") or {}

    checks = {
        "winner_matches_rerun": winner_ok,
        "executed_cost_matches_winner": cost_ok,
        "independent_no_worse_than_disclosed": bool(independent_no_worse),
        "candidates_root_matches": root_ok,
        "within_limit": within_limit,
        "split_matches_agreed_policy": split_ok,
        "proof_no_worse_than_best_disclosed": bool(be.get("user_no_worse_than_best_disclosed")),
        "proof_within_limit": bool(be.get("user_within_limit")),
        "proof_split_matches_policy": bool(be.get("split_matches_policy")),
        "surplus_splits_sum_exact": bool(sr.get("splits_sum_exact")),
        "surplus_effective_fee_non_positive": bool(sr.get("user_effective_fee_vs_limit_non_positive")),
    }

    # nonce-binding のオフライン照合（response が binding + nonce を載せている場合のみ）。
    # §4 step 7：binding_digest(binding) が署名済み nonce と一致し、binding の会計フィールド
    # （policyHash/objective/maxAmount）が response と合意ポリシーに整合すること。
    binding = response.get("binding")
    nonce = response.get("nonce")
    if binding is not None and nonce is not None:
        checks["binding_digest_matches_nonce"] = binding_digest(binding) == nonce
        checks["binding_policy_hash_matches"] = (
            binding.get("splitPolicyHash") == split_policy_hash(agreed_policy)
        )
        checks["binding_objective_matches"] = binding.get("objective") == objective
        checks["binding_max_amount_matches"] = str(binding.get("maxAmount")) == str(max_amount)

    return checks
