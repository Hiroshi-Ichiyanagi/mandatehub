"""
signers — 本物の EIP-712 署名実装（任意の `[evm]` extra）。

コアはこのパッケージを import しない。third-party 暗号（eth-account）はここだけに隔離され、
遅延 import される。`pip install mandatehub[evm]` で有効化。
"""

from mandatehub.signers.eth_account_signer import EthAccountSigner, MissingExtraError

__all__ = ["EthAccountSigner", "MissingExtraError"]
