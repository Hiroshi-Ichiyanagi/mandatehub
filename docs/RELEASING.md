# Releasing mandatehub (R4 — PyPI)

How a version of mandatehub is cut and published to PyPI. Publishing uses **PyPI Trusted
Publishing (OIDC)** — no API token is ever stored in the repo; the
[`release.yml`](../.github/workflows/release.yml) workflow mints a short-lived token at run
time when a GitHub Release is published.

This follows [`OPERATIONS.md`](OPERATIONS.md): outward-facing / distribution steps are the
**owner's** call and get explicit confirmation. The one-time setup below is owner action; the
per-release flow is a checklist.

## One-time setup (owner action — do once)

These three steps are the human gate for R4. They cannot be automated from the repo because
they require ownership of the PyPI project and the GitHub environment.

1. **Reserve the project on PyPI.** Sign in at <https://pypi.org> and either create the
   `mandatehub` project or (recommended) configure the trusted publisher *before* the first
   upload so the very first release is token-free — PyPI supports a
   [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   for a not-yet-existing project.

2. **Configure the Trusted Publisher (OIDC).** On the project's *Publishing* settings
   (<https://pypi.org/manage/project/mandatehub/settings/publishing/>, or the pending-publisher
   form) add a GitHub publisher with **exactly**:

   | field | value |
   | ----- | ----- |
   | Owner | `Hiroshi-Ichiyanagi` |
   | Repository | `mandatehub` |
   | Workflow name | `release.yml` |
   | Environment | `pypi` |

   Docs: <https://docs.pypi.org/trusted-publishers/>. The environment name **must** match the
   `environment: pypi` in `release.yml`.

3. **Create the `pypi` GitHub environment.** In the repo → *Settings → Environments → New
   environment* → name it `pypi`. Optionally add a required reviewer (yourself) so every
   publish needs a manual approval click — a good fail-closed default for a step that pushes
   an artifact to the world.

For `mandatehub[evm]` there is nothing extra to do: the `evm` extra is declared in
`pyproject.toml` and ships inside the same distribution.

## Per-release checklist

1. **Confirm green + reproduce the pipeline locally.** From a clean checkout of `main`:

   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install --upgrade pip build twine
   python -m build                       # sdist + wheel → dist/
   twine check dist/*                     # metadata + long-description render
   pip install "dist/"*.whl"[test]"
   python -m pytest -q                    # 240 passed
   python -c "import mandatehub; print(mandatehub.__version__)"
   ```

   This is exactly what `release.yml`'s `build` job runs in CI; a local pass means the
   published wheel is the tested wheel. *(Verified 2026-07-21: build + `twine check` both
   PASSED, wheel imports, `__version__ == 0.1.0`.)*

2. **Bump the version** in `pyproject.toml` (`[project].version`) and mirror it in
   `mandatehub/__init__.py` (`__version__`). Keep them equal — the CI *Import smoke* step and
   the `to_public_summary` proofs surface `__version__`. Update
   [`CHANGELOG.md`](../CHANGELOG.md) for the new version.

3. **Land it via PR** (no direct push to `main`, per OPERATIONS §3.1). Merge once CI is green.

4. **Tag + GitHub Release.** Draft a Release with tag `vX.Y.Z` (e.g. `v0.1.0`) matching the
   `pyproject.toml` version. **Publishing the Release** triggers `release.yml`.

5. **Watch the workflow.** `release.yml` runs `build` (build → `twine check` → install the
   built wheel → `pytest`), then `publish` (OIDC → `pypa/gh-action-pypi-publish`). If you set a
   required reviewer on the `pypi` environment, approve the run when prompted.

6. **Verify the publish.**

   ```bash
   pip install mandatehub==X.Y.Z          # from PyPI, fresh venv
   python -c "import mandatehub; print(mandatehub.__version__)"
   pip install "mandatehub[evm]==X.Y.Z"   # optional EVM extra resolves
   ```

   Confirm the project page renders the README at <https://pypi.org/project/mandatehub/>.

## Notes

- **Trusted publishing over tokens.** No `PYPI_API_TOKEN` secret exists or should be added;
  the OIDC exchange is scoped to this repo + workflow + environment and expires in minutes.
- **Attestations.** `pypa/gh-action-pypi-publish` emits PEP 740 attestations by default, so
  each release is provenance-signed without extra config (the R3 "signed-release" piece).
- **`workflow_dispatch`.** `release.yml` also allows a manual run for dry-runs, but a real
  publish should go through a GitHub Release so the tag, changelog, and artifact line up.
- **Never publish uncommitted state.** Build from a clean checkout of the tagged commit; `dist/`
  is git-ignored so a stray local build can't leak into the artifact.
