#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openunit -- reference implementation (engine)

A deterministic, publicly verifiable, population-weighted *unit of account*.
It does not issue money and it does not settle payments. It only *measures*:
given the same pinned inputs, anyone recomputes the same value and the same
hashes, byte for byte.

Method versions
  v0     one person, one vote          weight_i = pop_i / sum(pop)
  v0.1   same method, real pinned data  (UN population + ECB reference FX)
  v0.2   PPP-aware                       see SPEC.md sections 2 and 7

Mechanism (see SPEC.md):
  - one person, one vote : weight_i proportional to population_i (optionally
                           x a per-entry `weight_multiplier`, default 1)
  - frequency separation : weights pinned at a low-frequency vintage; value
                           read from a separate (e.g. daily, or PPP) FX leg
  - vintage pinning      : fixed currency amounts -- the population version of
                           the SDR fixed-basket method

Determinism guarantees:
  - stdlib only (json, hashlib, decimal)
  - exact arithmetic with decimal.Decimal (prec=50, ROUND_HALF_EVEN) inside an
    isolated localcontext; numbers parsed from strings, never from floats
  - never reads the wall clock (no datetime / time)
  - SHA-256 over canonical JSON for input_digest and artifact_hash

Backward compatibility:
  A spec whose basket entries carry no `weight_multiplier` produces output that
  is byte-for-byte identical to openunit v0 (the extra weighting fields are
  emitted only when at least one multiplier is present).

"Don't trust me. Verify me."
"""

import json
import hashlib
from decimal import Decimal, localcontext, ROUND_HALF_EVEN

PRECISION = 50
ROUNDING = ROUND_HALF_EVEN

METHOD = "openunit"
METHOD_VERSION = "v0"   # default for specs that do not pin their own version


# --- canonicalization & hashing ---------------------------------------------
def canonical(obj):
    """Deterministic JSON encoding used for every hash."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256(obj):
    return "sha256:" + hashlib.sha256(canonical(obj)).hexdigest()


# --- core computation --------------------------------------------------------
def _D(x):
    """Exact Decimal from a string. Never build a Decimal from a float."""
    return Decimal(str(x))


def _compute(spec):
    """Pure function: spec -> (baseline_value, value, rows). No I/O, no clock."""
    basket = spec["basket"]
    use_mult = any("weight_multiplier" in e for e in basket)

    with localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.rounding = ROUNDING

        total_pop = Decimal(0)
        total_eff = Decimal(0)
        for e in basket:
            pop = _D(e["population"])
            mult = _D(e.get("weight_multiplier", "1"))
            if pop < 0:
                raise ValueError("population must be >= 0 for %s" % e.get("code"))
            total_pop += pop
            total_eff += pop * mult
        if total_pop <= 0 or total_eff <= 0:
            raise ValueError("total population (and effective weight) must be > 0")

        baseline_value = Decimal(0)
        valuation_value = Decimal(0)
        tmp = []
        for e in basket:
            pop = _D(e["population"])
            mult = _D(e.get("weight_multiplier", "1"))
            fx_b = _D(e["fx_baseline_usd_per_unit"])
            fx_v = _D(e["fx_valuation_usd_per_unit"])
            if fx_b <= 0 or fx_v <= 0:
                raise ValueError("fx rates must be > 0 for %s" % e.get("code"))

            pop_share = pop / total_pop           # pure population share
            weight = (pop * mult) / total_eff     # effective (pinned) weight
            amount = weight / fx_b                 # fixed quantity pinned at vintage
            contrib_b = amount * fx_b              # == weight, by construction
            contrib_v = amount * fx_v

            baseline_value += contrib_b
            valuation_value += contrib_v
            tmp.append((e, pop_share, mult, weight, amount, contrib_b, contrib_v))

        rows = []
        for (e, pop_share, mult, weight, amount, contrib_b, contrib_v) in tmp:
            row = {
                "code": e["code"],
                "name": e.get("name", e["code"]),
                "population_share": str(pop_share),
                "fixed_amount": str(amount),
                "realized_weight_at_pin": str(contrib_b / baseline_value),
                "realized_weight_at_valuation": str(contrib_v / valuation_value),
            }
            if use_mult:
                # Emitted only when weighting differs from pure population, so
                # that plain v0 specs stay byte-for-byte identical to openunit v0.
                row["weight_multiplier"] = str(mult)
                row["effective_weight"] = str(weight)
            rows.append(row)

        return str(baseline_value), str(valuation_value), rows


# --- public API --------------------------------------------------------------
def build_artifact(spec):
    """Build a deterministic, self-describing artifact from a spec dict."""
    baseline_value_s, value_s, rows = _compute(spec)

    core = {
        "method": METHOD,
        "method_version": spec.get("method_version", METHOD_VERSION),
        "numeraire": spec.get("numeraire", "USD"),
        "weight_vintage_label": spec["weight_vintage"]["label"],
        "weight_basis": spec["weight_vintage"]["basis"],
        "baseline_value_usd": baseline_value_s,   # == 1 by construction
        "value_usd": value_s,                     # 1 openunit in the numeraire
        "basket": rows,
        "input_digest": sha256(spec),
    }
    artifact = dict(core)
    artifact["artifact_hash"] = sha256(core)
    return artifact


def verify_artifact(artifact, spec, explain=False):
    """True iff `artifact` is internally consistent AND reproduces `spec`."""
    reasons = []

    if artifact.get("input_digest") != sha256(spec):
        reasons.append("input_digest does not match spec")

    core = {k: v for k, v in artifact.items() if k != "artifact_hash"}
    if sha256(core) != artifact.get("artifact_hash"):
        reasons.append("artifact_hash does not match artifact content")

    if build_artifact(spec) != artifact:
        reasons.append("artifact does not reproduce from spec")

    ok = not reasons
    if explain and not ok:
        for r in reasons:
            print("  - " + r)
    return ok


# --- demo --------------------------------------------------------------------
def _demo(path="sample_input.json"):
    with open(path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    artifact = build_artifact(spec)
    ok = verify_artifact(artifact, spec, explain=True)

    q = Decimal("0.000001")
    value = Decimal(artifact["value_usd"]).quantize(q)
    has_mult = any("weight_multiplier" in r for r in artifact["basket"])
    print("openunit reference demo  (%s)" % artifact["method_version"])
    print("-" * 62)
    print("1 openunit  = %s %s" % (value, artifact["numeraire"]))
    print("weight basis: %s   vintage: %s"
          % (artifact["weight_basis"], artifact["weight_vintage_label"]))
    print()
    header = "  code   pop.share    realized@pin   realized@val"
    if has_mult:
        header += "    eff.weight"
    print(header)
    rows = sorted(artifact["basket"],
                  key=lambda r: Decimal(r["realized_weight_at_pin"]), reverse=True)
    for r in rows:
        ps = Decimal(r["population_share"]).quantize(q)
        rp = Decimal(r["realized_weight_at_pin"]).quantize(q)
        rv = Decimal(r["realized_weight_at_valuation"]).quantize(q)
        line = "  %-5s  %9s    %12s   %12s" % (r["code"], ps, rp, rv)
        if has_mult:
            ew = Decimal(r["effective_weight"]).quantize(q)
            line += "  %12s" % ew
        print(line)
    print()
    print("input_digest : %s" % artifact["input_digest"])
    print("artifact_hash: %s" % artifact["artifact_hash"])
    print("-" * 62)
    print("verify_artifact(...) -> %s" % ok)


if __name__ == "__main__":
    import sys
    _demo(sys.argv[1] if len(sys.argv) > 1 else "sample_input.json")
