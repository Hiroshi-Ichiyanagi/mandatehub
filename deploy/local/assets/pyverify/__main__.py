#!/usr/bin/env python3
"""CLI: `python3 -m pyverify <bundle-dir>` (zero third-party deps).

Independent, offline, public verifier. Exit codes mirror the Rust public
verifier `govern-verify`:
  0 OFFLINE-VERIFIED · 20 parent · 21 child (incl. receipt/mandate/approval
  Ed25519) · 22 reverse-ref · 24 witness · 33 STH · 34 consistency.

This verifies cryptographic authenticity and structural integrity. It does NOT
re-evaluate governance policy (budget/sequence/exclusion/info-flow/obligation) —
that is the role of the full (non-public) govern engine.
"""

import sys
from pathlib import Path

from . import verify_bundle


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: python3 -m pyverify <bundle-dir>", file=sys.stderr)
        return 2
    d = Path(argv[1])
    code, detail = verify_bundle(d)
    if code == 0:
        print(f"pyverify: OFFLINE-VERIFIED (exit 0) — {detail}")
    else:
        print(f"pyverify: OFFLINE-VERIFY FAIL (exit {code}) — {detail}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
