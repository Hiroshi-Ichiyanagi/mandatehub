"""
intent — インテント（意図）ベースの自律決済オーソライゼーション + その証明（④）。

「事前に予算枠（Mandate）を信託し、枠内で自律エージェントが決済を繰り返す」モデルの
検証コア。velocity/epoch 上限・spend policy・セッション鍵（サブ委任枠）・バッチ決済・
ライフサイクル（pause/resume/revoke/top-up）・nonce/replay 保護を備え、best-execution
橋渡し（settle_via_auction）で ③ の最良執行 + サープラス回収と統合する。

決済実行そのもの（オンチェーン）は範囲外。ここが担保するのは「枠を一度も超えて
いない」ことをオフラインで検証できることである。
"""

from mandatehub.intent.audit_asof import audit_root_as_of
from mandatehub.intent.errors import MandateError, SettlementIntegrityError
from mandatehub.intent.lifecycle import MandateLifecycleView, MandateState
from mandatehub.intent.mandate import DENIAL_ORDER, IntentSettlementEngine, Mandate
from mandatehub.intent.policy import EpochSpec, SpendPolicy
from mandatehub.intent.proofs import (
    MandatePortfolioProof,
    MandatePortfolioProofGenerator,
    ProofOfMandate,
    ProofOfMandateGenerator,
)
from mandatehub.intent.results import (
    AuctionSettlementResult,
    BatchSettlementResult,
    IntentRequest,
    IntentSettlementResult,
)
from mandatehub.intent.settlement import SettlementRecord

__all__ = [
    # engine + mandate
    "IntentSettlementEngine",
    "Mandate",
    "DENIAL_ORDER",
    # policy / epoch
    "SpendPolicy",
    "EpochSpec",
    # lifecycle
    "MandateState",
    "MandateLifecycleView",
    # results / requests
    "IntentSettlementResult",
    "IntentRequest",
    "BatchSettlementResult",
    "AuctionSettlementResult",
    "SettlementRecord",
    # proofs
    "ProofOfMandate",
    "ProofOfMandateGenerator",
    "MandatePortfolioProof",
    "MandatePortfolioProofGenerator",
    "audit_root_as_of",
    # errors
    "MandateError",
    "SettlementIntegrityError",
]
