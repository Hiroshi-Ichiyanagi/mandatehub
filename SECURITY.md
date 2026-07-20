# Security Policy

## Status — read this first

mandatehub is **early, unproven, and has no production adoption**. It is a verification core
and protocol library, not an audited payment system. The internal adversarial reviews in this
project's history are **not** a substitute for an independent security audit.

**Do not use mandatehub to move real funds on mainnet** until it has had an independent
security review, production hardening (key management, durable storage, auth, monitoring), and
a legal/compliance review. See [ROADMAP.md](ROADMAP.md) (hard gates H1–H3). Nothing here is
legal or financial advice.

The core has **no third-party runtime dependencies** (standard library only); real EVM signing
lives behind the optional `[evm]` extra (`eth-account`). Private keys must come from an
environment/keystore/KMS, are consumed only at signer construction, and are never logged or
included in error messages.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue for a
security bug.

- Preferred: open a **GitHub private security advisory** ("Report a vulnerability" under the
  repository's Security tab), or
- Email the maintainer listed on the GitHub profile.

Include a description, affected version/commit, and a minimal reproduction if possible. We aim
to acknowledge within a few days. Please give us reasonable time to address the issue before
any public disclosure.

## Scope

In scope: the ledger/proof primitives, the intent/mandate engine, the execution/surplus logic,
and the x402 facilitator + client (verify/settle, payload construction, header handling,
redirect/TLS handling, secret redaction).

Out of scope (by design, for now): on-chain execution correctness of an external facilitator,
the security of third-party facilitators/RPCs, and anything requiring the hard gates above.
