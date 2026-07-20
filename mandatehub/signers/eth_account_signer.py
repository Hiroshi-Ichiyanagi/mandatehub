"""
signers/eth_account_signer.py — eth-account を使う本物の EIP-712 署名（任意の [evm] extra）。

eth_account が無ければ import は成功するが、構築時に MissingExtraError を投げる
（コアは stdlib-only のまま、暗号はここだけに隔離）。秘密鍵は env/keystore から構築時のみ
受け取り、ログにも例外にも載せない。
"""

from __future__ import annotations

from typing import Any

try:  # third-party、[evm] extra
    from eth_account import Account as _Account

    _HAVE_ETH_ACCOUNT = True
except ImportError:  # pragma: no cover - depends on optional extra
    _Account = None  # type: ignore[assignment]
    _HAVE_ETH_ACCOUNT = False


class MissingExtraError(RuntimeError):
    """[evm] extra（eth-account）が未インストールのとき。"""


class EthAccountSigner:
    """eth-account による EIP-712 署名者。`pip install mandatehub[evm]` が必要。"""

    def __init__(self, private_key: str | None = None) -> None:
        if not _HAVE_ETH_ACCOUNT:
            raise MissingExtraError(
                "EthAccountSigner requires the optional dependency 'eth-account'. "
                "Install it with: pip install 'mandatehub[evm]'"
            )
        if not private_key:
            raise ValueError("private_key is required (load it from env / keystore, never hardcode)")
        self._acct = _Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._acct.address

    def sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        # eth-account >= 0.10: Account.sign_typed_data(private_key, full_message=...)
        signed = self._acct.sign_typed_data(full_message=typed_data)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig
