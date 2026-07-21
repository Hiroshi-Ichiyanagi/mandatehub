"""
x402 — mandatehub を委任枠 gate + 証明レイヤとした x402 互換ファシリテーター（HTTP 402）。

x402（Coinbase の HTTP ネイティブ支払い規約）の役割・語彙・facilitator の verify/settle を
踏襲する。決済実行は SettlementAdapter で差し替え可能：既定は自己完結の元帳記帳（実マネー無し）、
将来は実際の x402 ファシリテーター（例 Base 上の USDC）へ委譲するアダプタを差し込む。
"""

from mandatehub.x402.facilitator import (
    DEFAULT_NETWORK,
    Facilitator,
    LedgerSettlementAdapter,
    SettlementAdapter,
)
from mandatehub.x402.http402 import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    decode_payload,
    decode_requirements,
    decode_settle_response,
    encode_payload,
    encode_requirements,
    encode_settle_response,
    serve_once,
)
from mandatehub.x402.types import (
    SCHEME_EXACT,
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)

# --- Phase 3: best-exec scheme (expose 3 as an x402 scheme; offline accounting layer) ---
from mandatehub.x402.best_exec import (
    SCHEME_BEST_EXEC,
    BestExecFacilitator,
    BestExecParams,
    BestExecPayload,
    BestExecResult,
    InLedgerSettlementAdapter,
    StubVerifier,
    binding_digest,
    binding_preimage,
    build_best_exec_payload,
    split_policy_hash,
    verify_best_exec_response,
)

# --- Phase 2: real x402 v1 client (exact/EVM) ---
from mandatehub.x402.eip712 import (
    BASE_SEPOLIA_CHAIN_ID,
    BASE_MAINNET_CHAIN_ID,
    BASE_MAINNET_USDC,
    BASE_MAINNET_USDC_DOMAIN,
    BASE_SEPOLIA_USDC,
    USDC_DECIMALS,
    build_transfer_with_authorization,
    chain_id_for,
)
from mandatehub.x402.exact_evm import ExactEvmPayloadBuilder
from mandatehub.x402.remote import FacilitatorError, RemoteFacilitatorAdapter
from mandatehub.x402.signer import NullSigner, Signer, SignerError, StubSigner
from mandatehub.x402.wire import (
    EIP3009Authorization,
    ExactEvmPayload,
    FacilitatorSettleResult,
    FacilitatorVerifyResult,
    X402PaymentPayload,
    X402PaymentRequirements,
    decode_x_payment,
    decode_x_payment_response,
    encode_x_payment,
    encode_x_payment_response,
)

__all__ = [
    # Phase 1: mandatehub's own facilitator (ledger/mock)
    "Facilitator",
    "SettlementAdapter",
    "LedgerSettlementAdapter",
    "DEFAULT_NETWORK",
    "PaymentRequirements",
    "PaymentPayload",
    "VerifyResponse",
    "SettleResponse",
    "SCHEME_EXACT",
    "serve_once",
    "encode_requirements",
    "decode_requirements",
    "encode_payload",
    "decode_payload",
    "encode_settle_response",
    "decode_settle_response",
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_PAYMENT_SIGNATURE",
    "HEADER_PAYMENT_RESPONSE",
    # Phase 2: real x402 v1 client (exact/EVM)
    "RemoteFacilitatorAdapter",
    "FacilitatorError",
    "ExactEvmPayloadBuilder",
    "Signer",
    "NullSigner",
    "StubSigner",
    "SignerError",
    "X402PaymentRequirements",
    "X402PaymentPayload",
    "ExactEvmPayload",
    "EIP3009Authorization",
    "FacilitatorVerifyResult",
    "FacilitatorSettleResult",
    "encode_x_payment",
    "decode_x_payment",
    "encode_x_payment_response",
    "decode_x_payment_response",
    "build_transfer_with_authorization",
    "chain_id_for",
    "BASE_SEPOLIA_CHAIN_ID",
    "BASE_MAINNET_CHAIN_ID",
    "BASE_MAINNET_USDC",
    "BASE_MAINNET_USDC_DOMAIN",
    "BASE_SEPOLIA_USDC",
    "USDC_DECIMALS",
    # Phase 3: best-exec scheme (offline accounting layer)
    "SCHEME_BEST_EXEC",
    "BestExecFacilitator",
    "BestExecParams",
    "BestExecPayload",
    "BestExecResult",
    "InLedgerSettlementAdapter",
    "StubVerifier",
    "build_best_exec_payload",
    "verify_best_exec_response",
    "binding_preimage",
    "binding_digest",
    "split_policy_hash",
]
