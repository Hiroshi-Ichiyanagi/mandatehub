"""
x402/exact_evm.py — x402 `exact`/EVM の PaymentPayload を組み立てる。

要件（PaymentRequirements）と Signer から EIP-3009 authorization を作り、EIP-712 に署名して
X402PaymentPayload を返す。セキュリティ要件（docs/X402.md §5）を焼き込む：
  - nonce は毎回 CSPRNG の 32byte（再利用しない）
  - EIP-712 domain は要件の extra.name / extra.version / asset / network から厳密に組む
  - value == maxAmountRequired, to == payTo, validBefore = now + min(timeout, mandate_ttl)
"""

from __future__ import annotations

import secrets
import time
from typing import Callable

from mandatehub.x402.eip712 import build_transfer_with_authorization, chain_id_for
from mandatehub.x402.signer import Signer
from mandatehub.x402.wire import (
    EIP3009Authorization,
    ExactEvmPayload,
    X402PaymentPayload,
    X402PaymentRequirements,
)


class ExactEvmPayloadBuilder:
    def __init__(
        self,
        signer: Signer,
        *,
        network: str = "base-sepolia",
        chain_id: int | None = None,
        clock: Callable[[], float] = time.time,
        nonce_source: Callable[[], bytes] = lambda: secrets.token_bytes(32),
        valid_after_skew: int = 60,
    ) -> None:
        self._signer = signer
        self._network = network
        self._chain_id = chain_id if chain_id is not None else chain_id_for(network)
        self._clock = clock
        self._nonce_source = nonce_source
        self._valid_after_skew = valid_after_skew

    def build(
        self, requirements: X402PaymentRequirements, *, mandate_ttl: int | None = None
    ) -> X402PaymentPayload:
        extra = requirements.extra or {}
        if "name" not in extra or "version" not in extra:
            raise ValueError("requirements.extra must carry EIP-712 domain {name, version} for exact/EIP-3009")
        # Fail-closed on a network mismatch: this builder signs over its configured chain_id
        # and advertises its network slug, so a differing requirements.network would sign the
        # wrong chain / advertise the wrong slug. Construct a builder per network instead.
        if requirements.network != self._network:
            raise ValueError(
                f"builder network {self._network!r} != requirements.network {requirements.network!r}"
            )

        now = int(self._clock())
        nonce = self._nonce_source()
        if len(nonce) != 32:
            raise ValueError("nonce must be 32 bytes")
        ttl = requirements.max_timeout_seconds if mandate_ttl is None else min(requirements.max_timeout_seconds, mandate_ttl)

        authorization = EIP3009Authorization(
            from_=self._signer.address,
            to=requirements.pay_to,
            value=requirements.max_amount_required,
            valid_after=str(now - self._valid_after_skew),
            valid_before=str(now + ttl),
            nonce="0x" + nonce.hex(),
        )
        typed_data = build_transfer_with_authorization(
            authorization,
            domain_name=extra["name"],
            domain_version=extra["version"],
            chain_id=self._chain_id,
            verifying_contract=requirements.asset,
        )
        signature = self._signer.sign_typed_data(typed_data)
        return X402PaymentPayload(
            scheme="exact",
            network=self._network,
            payload=ExactEvmPayload(signature=signature, authorization=authorization),
        )
