"""
signers — 本物の EIP-712 署名実装（任意の `[evm]` extra）。

コアはこのパッケージを import しない。third-party 暗号（eth-account）はここだけに隔離され、
遅延 import される。`pip install mandatehub[evm]` で有効化。
"""

from mandatehub.signers.cdp_auth import (
    CDP_FACILITATOR_URL,
    MissingCdpExtraError,
    cdp_header_hook,
    cdp_header_hook_from_file,
)
from mandatehub.signers.eth_account_signer import EthAccountSigner, MissingExtraError

__all__ = [
    "CDP_FACILITATOR_URL",
    "EthAccountSigner",
    "MissingCdpExtraError",
    "MissingExtraError",
    "cdp_header_hook",
    "cdp_header_hook_from_file",
]
