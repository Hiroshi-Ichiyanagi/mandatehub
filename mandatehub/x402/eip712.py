"""
x402/eip712.py — EIP-712 typed-data の組み立て（標準ライブラリのみ・署名はしない）。

Signer が消費する typed-data 辞書（domain/types/primaryType/message）を作るだけで、
keccak も secp256k1 も含まない（third-party import なし）→ コアは stdlib-only を維持。

Base Sepolia の定数はオンチェーンで検証済み（docs/X402.md）だが、実行時は
PaymentRequirements の asset / extra.name / extra.version / network から読むことで
他トークン・他ネットワークにも対応する（定数はあくまで既定値）。
"""

from __future__ import annotations

from typing import Any

from mandatehub.x402.wire import EIP3009Authorization

# Base Sepolia（オンチェーン eth_call + domain separator 再計算で確認済み）
BASE_SEPOLIA_CHAIN_ID = 84532
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6
# 参考: 検証済み domain separator = 0x71f17a3b2ff373b803d70a5a07c046c1a2bc8e89c09ef722fcb047abe94c9818

# Base MAINNET（2026-07-21 オンチェーン検証済み: name()/version()/DOMAIN_SEPARATOR() を
# eth_call で読み、オフライン再計算と byte-exact 一致を確認）。
# ⚠️ EIP-712 domain name は "USD Coin"（Sepolia の "USDC" とは異なる）。Sepolia の
# extra {"name":"USDC"} を mainnet に流用すると署名が全て無効になる。
BASE_MAINNET_CHAIN_ID = 8453
BASE_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_MAINNET_USDC_DOMAIN = {"name": "USD Coin", "version": "2"}
# 参考: 検証済み domain separator = 0x02fa7265e7c5d81118673727957699e4d68f74cd74b7db77da710fe8a2c7834f

_CHAIN_IDS: dict[str, int] = {
    "base-sepolia": 84532,
    "base": 8453,
    # x402 v2 は CAIP-2 スラッグを使う（同一チェーンの別表記）
    "eip155:84532": 84532,
    "eip155:8453": 8453,
}


def chain_id_for(network: str) -> int:
    try:
        return _CHAIN_IDS[network]
    except KeyError:
        raise ValueError(f"unknown network slug: {network!r} (add it to _CHAIN_IDS)") from None


def build_transfer_with_authorization(
    authorization: EIP3009Authorization,
    *,
    domain_name: str,
    domain_version: str,
    chain_id: int,
    verifying_contract: str,
) -> dict[str, Any]:
    """TransferWithAuthorization の EIP-712 typed-data 辞書を組み立てる。

    ワイヤでは value/validAfter/validBefore は文字列だが、typed-data メッセージでは
    int、nonce は bytes32（bytes）になる点に注意。
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": domain_name,
            "version": domain_version,
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "from": authorization.from_,
            "to": authorization.to,
            "value": int(authorization.value),
            "validAfter": int(authorization.valid_after),
            "validBefore": int(authorization.valid_before),
            "nonce": bytes.fromhex(authorization.nonce[2:] if authorization.nonce.startswith("0x") else authorization.nonce),
        },
    }
