"""Preflight for the live smoke test — validate the client wiring OFFLINE, no network, no key.

Run this BEFORE examples/x402_live_smoke.py to catch config mistakes without touching the
network or spending a real facilitator call. It checks, using a StubSigner by default (so no
private key is needed), that:

  1. the required env vars are set (MANDATEHUB_FACILITATOR_URL, MANDATEHUB_PAY_TO),
  2. the facilitator URL is accepted by RemoteFacilitatorAdapter's https guard,
  3. the exact/EVM payload BUILDS and base64-encodes (the X-PAYMENT the smoke test would send),
  4. — if MANDATEHUB_AGENT_PRIVATE_KEY is set and `mandatehub[evm]` is installed — the REAL
     EthAccountSigner loads and derives an address (still no network call).

It NEVER calls /verify or /settle and NEVER needs testnet USDC. Exit code 0 = ready to run the
smoke test; non-zero = fix the reported problem first.

    export MANDATEHUB_FACILITATOR_URL=https://x402.org/facilitator
    export MANDATEHUB_PAY_TO=0xYourRecipient
    # optional, to preflight the real signer too:
    # export MANDATEHUB_AGENT_PRIVATE_KEY=0x...   (needs: pip install 'mandatehub[evm]')
    python examples/x402_live_preflight.py
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

from mandatehub.x402 import (
    BASE_SEPOLIA_USDC,
    ExactEvmPayloadBuilder,
    FacilitatorError,
    RemoteFacilitatorAdapter,
    StubSigner,
    X402PaymentRequirements,
    encode_x_payment,
)

CHECK = "✓"
CROSS = "✗"


def _fail(msg: str) -> None:
    print(f"  {CROSS} {msg}")
    sys.exit(1)


def main() -> None:
    print("x402 live preflight (offline; no network, no testnet value) …\n")

    # 1. required env ---------------------------------------------------------
    url = os.environ.get("MANDATEHUB_FACILITATOR_URL")
    pay_to = os.environ.get("MANDATEHUB_PAY_TO")
    if not url:
        _fail("MANDATEHUB_FACILITATOR_URL is not set")
    if not pay_to:
        _fail("MANDATEHUB_PAY_TO is not set")
    amount = os.environ.get("MANDATEHUB_AMOUNT", "10000")  # 0.01 USDC (6 decimals)
    network = os.environ.get("MANDATEHUB_NETWORK", "base-sepolia")
    asset = os.environ.get("MANDATEHUB_ASSET", BASE_SEPOLIA_USDC)
    if not (amount.isdigit() and int(amount) > 0):
        _fail(f"MANDATEHUB_AMOUNT must be a positive integer in minor units, got {amount!r}")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", pay_to):
        _fail(f"MANDATEHUB_PAY_TO must be a 0x-prefixed 20-byte hex address, got {pay_to!r}")
    print(f"  {CHECK} env present + well-formed (amount={amount} network={network})")

    # 2. facilitator URL passes the https guard (construction only, no call) --
    if not urlparse(url).hostname:
        _fail(f"MANDATEHUB_FACILITATOR_URL has no hostname: {url!r}")
    try:
        RemoteFacilitatorAdapter(url, network=network)
    except FacilitatorError as e:
        _fail(f"facilitator URL rejected: {e}")
    print(f"  {CHECK} facilitator URL accepted by the https guard")

    # 3. pick a signer: real if a key + [evm] are present, else the stub ------
    signer = StubSigner()
    signer_kind = "StubSigner (no key; wire-format check only)"
    priv = os.environ.get("MANDATEHUB_AGENT_PRIVATE_KEY")
    if priv:
        try:
            from mandatehub.signers import EthAccountSigner

            signer = EthAccountSigner(priv)
            signer_kind = "EthAccountSigner (real key)"
        except Exception as e:  # MissingExtraError if [evm] not installed, or bad key
            _fail(f"MANDATEHUB_AGENT_PRIVATE_KEY set but signer failed "
                  f"(need: pip install 'mandatehub[evm]'): {e}")
    print(f"  {CHECK} signer: {signer_kind}  address={signer.address}")

    # 4. the payload builds + base64-encodes (what the smoke test would send) --
    requirements = X402PaymentRequirements(
        scheme="exact", network=network, max_amount_required=amount, asset=asset,
        pay_to=pay_to, resource="https://example.invalid/preflight", max_timeout_seconds=60,
        extra={"name": "USDC", "version": "2"},
    )
    try:
        payload = ExactEvmPayloadBuilder(signer, network=network).build(requirements)
        header = encode_x_payment(payload)
    except Exception as e:
        _fail(f"payload build/encode failed: {e}")
    print(f"  {CHECK} exact/EVM payload builds + encodes ({len(header)}-byte X-PAYMENT header)")

    print(f"\n{CHECK} preflight passed — client wiring is well-formed.")
    if not priv:
        print("  (used StubSigner; set MANDATEHUB_AGENT_PRIVATE_KEY to preflight the real signer)")
    print("  Next: examples/x402_live_smoke.py (calls /verify against the real facilitator).")


if __name__ == "__main__":
    main()
