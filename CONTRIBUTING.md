# Contributing to mandatehub

Thanks for your interest. mandatehub is early and exploratory — issues, discussions, and PRs
are welcome. By contributing you agree your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.

## Development setup

```bash
pip install -e ".[test]"     # editable install + pytest
pip install -e ".[test,evm]" # add real EVM signing (eth-account) if touching the signer
python -m pytest -q          # run the full suite (should be all green)
```

- **Python 3.11+.** The **core has zero third-party runtime dependencies** (standard library
  only). The only optional dependency, `eth-account`, is isolated behind the `[evm]` extra and
  must be imported lazily — never from the core.

## Ground rules (what keeps this project coherent)

- **Determinism.** Proof/settlement generation takes an explicit time and must never read the
  wall clock (`datetime.now()`). The guards in `tests/test_superrich_guards.py` enforce this
  (static AST scan + a runtime `datetime.now`-forbidden pass); keep them green.
- **Integer money only.** All amounts are integer minor units; no floats.
- **Import discipline.** `execution/` must not import `intent/`; the single seam is
  `intent/bridge.py`. A test enforces this.
- **Offline-verifiable.** New proofs must reuse the Merkle + audit-chain primitives and be
  re-verifiable without trusting the operator.
- **Match the surrounding style.** Impl modules carry Japanese docstrings (as the codebase
  does); examples are in English. Frozen dataclasses for value objects.

## Pull requests

1. Add or update tests; the suite must stay green (`python -m pytest -q`).
2. Keep changes focused and describe the behavior change and why.
3. For anything touching the payment/settlement/signing paths, call out the security
   implications explicitly (see [SECURITY.md](SECURITY.md)).

## Reporting security issues

Do **not** open a public issue for a security vulnerability — see [SECURITY.md](SECURITY.md).
