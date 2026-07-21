#!/bin/sh
# Vendor the stdlib-only `mandatehub` package next to worker.py so `wrangler deploy`
# bundles it (it is not a Pyodide built-in). Re-run after any package change.
set -eu
cd "$(dirname "$0")"
SRC="../../mandatehub"
[ -d "$SRC" ] || { echo "package not found at $SRC (run from deploy/cloudflare/)"; exit 1; }
rm -rf mandatehub
# copy without caches; the package is pure stdlib so a plain copy is a working vendor
mkdir -p mandatehub
(cd "$SRC" && find . -name "*.py" -not -path "*/__pycache__/*" | while read -r f; do
  mkdir -p "../deploy/cloudflare/mandatehub/$(dirname "$f")"
  cp "$f" "../deploy/cloudflare/mandatehub/$f"
done)
echo "vendored: $(find mandatehub -name '*.py' | wc -l | tr -d ' ') files -> deploy/cloudflare/mandatehub/"
