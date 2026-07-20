"""Live smoke test — confirm a REAL x402 v1 facilitator accepts our exact/EVM payload.

Run this with YOUR credentials against a real facilitator (e.g. Base Sepolia) to verify the
v1 wire assumptions end-to-end. By default it calls only /verify (non-destructive). Set
MANDATEHUB_LIVE_SETTLE=1 to also /settle (moves real testnet value).

    pip install 'mandatehub[evm]'
    export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator   # or your CDP facilitator
    export MANDATEHUB_AGENT_PRIVATE_KEY=0x...                        # Base Sepolia agent key
    export MANDATEHUB_PAY_TO=0x...                                   # recipient address
    python examples/x402_live_smoke.py

Notes:
  - isValid=false with invalidReason "insufficient_funds" STILL confirms the v1 wire format
    is accepted (the facilitator parsed our payload and answered in v1) — just fund the wallet.
  - A CDP facilitator needs auth headers; supply them via RemoteFacilitatorAdapter(header_hook=...).
  - This script talks to the network, so it is NOT part of the offline test suite.
"""
from __future__ import annotations

import os
import sys

from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    ExactEvmPayloadBuilder,
    FacilitatorError,
    RemoteFacilitatorAdapter,
    X402PaymentRequirements,
    encode_x_payment,
)


def _require(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"missing required env var: {key}")
    return v


def main() -> None:
    url = _require("MANDATEHUB_FACILITATOR_URL")
    private_key = _require("MANDATEHUB_AGENT_PRIVATE_KEY")
    pay_to = _require("MANDATEHUB_PAY_TO")
    amount = os.environ.get("MANDATEHUB_AMOUNT", "10000")  # 0.01 USDC (6 decimals)
    network = os.environ.get("MANDATEHUB_NETWORK", "base-sepolia")
    asset = os.environ.get("MANDATEHUB_ASSET", BASE_SEPOLIA_USDC)

    try:
        from mandatehub.signers import EthAccountSigner

        signer = EthAccountSigner(private_key)
    except Exception as e:  # MissingExtraError if [evm] not installed
        sys.exit(f"signer setup failed (run: pip install 'mandatehub[evm]'): {e}")

    requirements = X402PaymentRequirements(
        scheme="exact", network=network, max_amount_required=amount, asset=asset,
        pay_to=pay_to, resource="https://example.invalid/smoke", max_timeout_seconds=60,
        extra={"name": "USDC", "version": "2"},
    )
    payload = ExactEvmPayloadBuilder(signer, network=network).build(requirements)
    print(f"payer:     {signer.address}")
    print(f"pay {int(amount) / 1e6:.6f} USDC -> {pay_to}  on {network}")
    print(f"X-PAYMENT: {encode_x_payment(payload)[:56]}…")

    adapter = RemoteFacilitatorAdapter(url, network=network)
    try:
        v = adapter.verify(payload, requirements)
    except FacilitatorError as e:
        sys.exit(f"/verify call failed: {e}")
    print(f"\n/verify -> isValid={v.is_valid}  invalidReason={v.invalid_reason}  payer={v.payer}")
    if not v.is_valid:
        print("  (a non-null invalidReason still confirms the facilitator accepted the v1 wire format)")

    if os.environ.get("MANDATEHUB_LIVE_SETTLE") == "1":
        print("\nMANDATEHUB_LIVE_SETTLE=1 -> calling /settle (moves real testnet value)…")
        try:
            s = adapter.settle(payload, requirements)
        except FacilitatorError as e:
            sys.exit(f"/settle call failed: {e}")
        print(f"/settle -> success={s.success}  transaction={s.transaction}  errorReason={s.error_reason}")
    else:
        print("\n(verify-only; set MANDATEHUB_LIVE_SETTLE=1 to settle real testnet value)")


if __name__ == "__main__":
    main()
