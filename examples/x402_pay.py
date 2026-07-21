"""Pay any x402 `exact` endpoint from the command line (the consumer side).

Point it at a URL that answers HTTP 402 (e.g. the live mandatehub service). It reads the
payment requirements, builds + signs an x402 `exact` (EIP-3009/EIP-712) payment with your
key, resends, and prints the resource + any `ProofOfMandate`. This is how a real agent
consumes a mandatehub (or any x402) resource server.

    pip install 'mandatehub[evm]'
    export MANDATEHUB_AGENT_PRIVATE_KEY=0x...        # a funded key on the endpoint's network
    python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote

    # dry run — see the 402 terms and what you'd pay, sign nothing, send nothing:
    python examples/x402_pay.py --quote-only https://mandatehub.obolpay.xyz/quote

The network/asset/amount all come from the server's 402 response, so the same client pays a
testnet or a mainnet endpoint unchanged. Real value moves only without --quote-only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from urllib.error import HTTPError

from mandatehub.x402 import (
    ExactEvmPayloadBuilder,
    X402PaymentRequirements,
    encode_x_payment,
)

# Public edges (Cloudflare) 403 the literal default urllib UA; always send a real one.
UA = {"User-Agent": "mandatehub-x402-client/1"}


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"_raw": e.read()[:200].decode("utf-8", "replace")}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    quote_only = "--quote-only" in argv
    if len(args) != 1:
        sys.exit("usage: python examples/x402_pay.py [--quote-only] <url>")
    url = args[0]

    status, body = _get(url)
    if status != 402:
        print(f"endpoint did not challenge with 402 (got {status}): {body}")
        return 1
    accepts = body.get("accepts") or []
    if not accepts:
        print(f"402 without 'accepts': {body}")
        return 1
    reqs = X402PaymentRequirements.from_wire(accepts[0])
    amount = int(reqs.max_amount_required) / 1e6
    print(f"402 challenge: pay up to {amount:.6f} (asset {reqs.asset[:10]}…) "
          f"on {reqs.network} to {reqs.pay_to[:10]}…")

    if quote_only:
        print("--quote-only: not signing or sending anything.")
        return 0

    key = os.environ.get("MANDATEHUB_AGENT_PRIVATE_KEY")
    if not key:
        sys.exit("set MANDATEHUB_AGENT_PRIVATE_KEY to pay (or use --quote-only)")
    try:
        from mandatehub.signers import EthAccountSigner
        signer = EthAccountSigner(key)
    except Exception as e:
        sys.exit(f"signer setup failed (pip install 'mandatehub[evm]'): {e}")

    header = encode_x_payment(ExactEvmPayloadBuilder(signer, network=reqs.network).build(reqs))
    print(f"paying as {signer.address} …")
    status, body = _get(url, {"X-PAYMENT": header})
    if status != 200:
        print(f"payment not accepted ({status}): "
              f"{body.get('mandateReason') or body.get('invalidReason') or body.get('errorReason') or body}")
        return 2

    print("200 OK — resource unlocked:")
    print(f"  data: {body.get('data')}")
    if "settlement" in body:
        print(f"  settlement tx: {body['settlement'].get('transaction')} "
              f"({body['settlement'].get('network')})")
    if "proofOfMandate" in body:
        p = body["proofOfMandate"]
        print(f"  proofOfMandate: remaining={p.get('remaining_cents')} "
              f"within_budget={p.get('is_within_budget')} "
              f"collateralized={p.get('is_collateralized')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
