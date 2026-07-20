"""
x402/signer.py — 署名の抽象（Signer プロトコル）と stdlib 実装。

実際の secp256k1/EIP-712 署名は third-party（eth-account 等）が要るため、その境界を
Signer プロトコルで切る。コアには鍵も暗号も入れない：
  - NullSigner  : 常に失敗（実運用で必ず本物の signer を設定させる）
  - StubSigner  : テスト用の決定的ダミー（固定アドレス + 固定 65byte 署名。暗号なし）

本物の署名は mandatehub/signers/eth_account_signer.py（任意の [evm] extra）が担う。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Signer(Protocol):
    """EIP-712 typed-data に署名する主体。"""

    @property
    def address(self) -> str:
        """署名者の checksummed 0x アドレス。"""
        ...

    def sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        """0x 前置の 65byte hex 署名を返す。"""
        ...


class SignerError(RuntimeError):
    """署名が構成されていない／実行できないとき。"""


class NullSigner:
    """常に失敗する signer。運用者に本物の signer 設定を強制する。"""

    @property
    def address(self) -> str:
        raise SignerError("no signer configured (use EthAccountSigner or inject a real Signer)")

    def sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        raise SignerError("no signer configured")


class StubSigner:
    """テスト用の決定的ダミー signer（暗号なし）。鍵も実ネットワークも不要で E2E を可能にする。"""

    def __init__(
        self,
        address: str = "0x857b06519E91e3A54538791bDbb0E22373e36b66",
        signature: str = "0x" + "2d" * 65,  # 65byte = 130 hex
    ) -> None:
        self._address = address
        self._signature = signature

    @property
    def address(self) -> str:
        return self._address

    def sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        return self._signature
