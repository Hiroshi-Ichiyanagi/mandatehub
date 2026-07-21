"""Run CDP's x402 Bazaar validation against a resource URL (the listing acceptance test).

    python deploy/local/validate_bazaar.py https://mandatehub.obolpay.xyz/quote-v2

Reads CDP creds from ~/.mandatehub-cdp.json. Prints VALID + any failing preflight checks.
Exit 0 when the endpoint is discoverable-eligible (all required checks pass).
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.exit("usage: python deploy/local/validate_bazaar.py <resource-url>")
    resource = argv[0]
    from cdp.auth import JwtOptions, generate_jwt
    cfg = json.loads((pathlib.Path.home() / ".mandatehub-cdp.json").read_text())
    host, path = "api.cdp.coinbase.com", "/platform/v2/x402/validate"
    jwt = generate_jwt(JwtOptions(api_key_id=cfg["keyId"], api_key_secret=cfg["keySecret"],
                                  request_method="POST", request_host=host, request_path=path))
    req = urllib.request.Request(
        f"https://{host}{path}", method="POST",
        data=json.dumps({"resource": resource, "method": "GET"}).encode(),
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
                 "User-Agent": "mandatehub-x402/1"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    fails = [c for c in d.get("preflight", []) if not c["passed"]]
    print(f"resource: {resource}")
    print(f"VALID: {d.get('valid')}  simulation: {json.dumps(d.get('simulation'))}")
    for c in fails:
        print(f"  [FAIL {c['severity']}] {c['check']}: {c.get('detail','')[:100]}")
    if d.get("valid"):
        print("✅ discoverable-eligible — all required checks pass")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
