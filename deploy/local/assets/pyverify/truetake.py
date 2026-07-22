"""TrueTake certificate verifier — an INDEPENDENT, pure-stdlib reimplementation
of `truetake-verify` (Rust) for cross-implementation fidelity. Third-party
dependencies: ZERO (stdlib + the vendored Ed25519 in `_ed25519.py`, + the
shared SCS / RFC 6962 reimplementations already in this package).

It verifies a `truetake/cert-0` certificate with public keys only, and returns
the SAME exit code as the Rust CLI / WASM verifier:

  0  CONFIRMED — every check holds.
  21 FAILED    — any authenticity/structural check fails.
   1 ERROR     — the input is not a parseable certificate object.

Checks (mirror of truetake_verify::verify_cert):
  1 receipt-signatures  every receipt Ed25519 sig verifies over
                        SHA256(SCS(receipt\\sig)); that digest is its leaf.
  2 merkle-root         RFC 6962 root over the receipt leaves equals merkle.root
                        and sth.root, and sth.size equals the receipt count.
  3 sth-signature       log key Ed25519 sig over SHA256(SCS({domain,root,tree_size})).
  4 content-consistency top-level work/materials/sth.ts/sth.process_order agree
                        with the SIGNED receipts.

Run:  python3 -m pyverify.truetake <cert.json>
"""

import base64
import hashlib
import json
import sys

from . import merkle, scs
from ._ed25519 import verify as ed25519_verify

EXIT_OK = 0
EXIT_FAIL = 21
EXIT_ERROR = 1

STH_DOMAIN = "govern/sth/v0"


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _verify_ed25519(pubkey_field, digest: bytes, sig_b64) -> bool:
    """Plain-Ed25519 verify: pubkey_field='ed25519:'+b64(32), sig=b64(64),
    message = the 32-byte digest. Never raises."""
    if not isinstance(pubkey_field, str) or not pubkey_field.startswith("ed25519:"):
        return False
    if not isinstance(sig_b64, str):
        return False
    try:
        pk = base64.b64decode(pubkey_field[len("ed25519:"):], validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
    except Exception:
        return False
    if len(pk) != 32 or len(sig) != 64:
        return False
    return ed25519_verify(pk, digest, sig)


def _receipt_core(receipt: dict) -> dict:
    """The receipt object minus its `sig` field (the hashed/signed/leafed core)."""
    return {k: v for k, v in receipt.items() if k != "sig"}


def verify_cert_obj(cert) -> tuple:
    if not isinstance(cert, dict):
        return EXIT_ERROR, "certificate is not a JSON object"
    receipts = cert.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        return EXIT_ERROR, "certificate has no receipts[]"

    # ---- Check 1: receipt signatures + leaves ----------------------------
    leaves = []
    sigs_ok = True
    for r in receipts:
        if not isinstance(r, dict):
            return EXIT_ERROR, "receipt is not an object"
        leaf = _sha256(scs.to_scs_bytes(_receipt_core(r)))
        if not _verify_ed25519(r.get("signer"), leaf, r.get("sig")):
            sigs_ok = False
        leaves.append(leaf)

    # ---- Check 2: Merkle root binding ------------------------------------
    root_hex = merkle.root(leaves).hex()
    sth = cert.get("sth") if isinstance(cert.get("sth"), dict) else {}
    merkle_root = (cert.get("merkle") or {}).get("root") if isinstance(cert.get("merkle"), dict) else None
    sth_root = sth.get("root")
    sth_size = sth.get("size")
    merkle_ok = (
        merkle_root == root_hex
        and sth_root == root_hex
        and isinstance(sth_size, int)
        and not isinstance(sth_size, bool)
        and sth_size == len(receipts)
    )

    # ---- Check 3: STH signature ------------------------------------------
    sth_ok = False
    if isinstance(sth_root, str) and isinstance(sth_size, int) and not isinstance(sth_size, bool):
        digest = _sha256(scs.to_scs_bytes(
            {"domain": STH_DOMAIN, "root": sth_root, "tree_size": sth_size}))
        sth_ok = _verify_ed25519(sth.get("log_pubkey"), digest, sth.get("sig"))

    # ---- Check 4: receipt index order (receipts[i].index == i) -----------
    index_ok = all(r.get("index") == i for i, r in enumerate(receipts))

    # ---- Check 5: content consistency ------------------------------------
    consistency_ok = _check_consistency(cert, receipts)

    checks = [
        ("receipt-signatures-ed25519", sigs_ok),
        ("merkle-root-rfc6962", merkle_ok),
        ("sth-signature-ed25519", sth_ok),
        ("receipt-index-order", index_ok),
        ("content-consistency", consistency_ok),
    ]
    # Additive: process-trust attestation, only if present (legacy/cert-0 skip it).
    if isinstance(cert.get("process_trust"), dict):
        checks.append(("process-trust", _check_process_trust(cert)))
    # Additive: human-signature observation, only if present (separate axis).
    if isinstance(cert.get("human_signature"), dict):
        checks.append(("human-signature", _check_human_signature(cert)))
    failed = [n for n, ok in checks if not ok]
    if not failed:
        return EXIT_OK, f"truetake/cert-0 verified: {len(receipts)} receipts, root {root_hex}"
    return EXIT_FAIL, "certificate failed: " + ", ".join(failed)


def _binds(receipts, kind):
    for r in receipts:
        if r.get("kind") == kind:
            return r.get("binds")
    return None


def _check_consistency(cert, receipts) -> bool:
    work = cert.get("work")
    wb = _binds(receipts, "work")
    if not isinstance(work, dict) or not isinstance(wb, dict):
        return False
    if work.get("hash") != wb.get("hash") or work.get("bytes") != wb.get("bytes") \
            or work.get("kind") != wb.get("kind"):
        return False

    materials = cert.get("materials")
    if not isinstance(materials, list):
        return False
    material_receipts = [r for r in receipts if r.get("kind") == "material"]
    if len(material_receipts) != len(materials):
        return False
    for i, m in enumerate(materials):
        rb = None
        for r in material_receipts:
            b = r.get("binds")
            if isinstance(b, dict) and b.get("material_index") == i:
                rb = b
                break
        if rb is None or not isinstance(m, dict):
            return False
        if m.get("hash") != rb.get("hash") or m.get("kind") != rb.get("kind") \
                or m.get("origin") != rb.get("origin") or m.get("ai") != rb.get("ai") \
                or m.get("note") != rb.get("note"):
            return False

    # process receipts <-> top-level process[] (chain extension; absent = legacy)
    process_top = cert.get("process")
    if process_top is not None and not isinstance(process_top, list):
        return False
    process_receipts = [r for r in receipts if r.get("kind") == "process"]
    process_count = len(process_top) if isinstance(process_top, list) else 0
    if process_count != len(process_receipts):
        return False
    if isinstance(process_top, list):
        for j, p in enumerate(process_top):
            rb = None
            for r in process_receipts:
                b = r.get("binds")
                if isinstance(b, dict) and b.get("process_index") == j:
                    rb = b
                    break
            if rb is None or not isinstance(p, dict):
                return False
            if p.get("type") != rb.get("type") or p.get("hash") != rb.get("hash") \
                    or p.get("ts") != rb.get("ts") or p.get("label") != rb.get("label") \
                    or p.get("ai") != rb.get("ai"):
                return False

    seal = _binds(receipts, "seal")
    sth = cert.get("sth") if isinstance(cert.get("sth"), dict) else {}
    if not isinstance(seal, dict):
        return False
    material_hashes = [m.get("hash") for m in materials]
    if seal.get("work_hash") != work.get("hash") \
            or seal.get("material_hashes") != material_hashes \
            or seal.get("ts") != sth.get("ts") \
            or seal.get("process_order") != sth.get("process_order"):
        return False

    # process_hashes binding: present iff there are process steps, equal in order.
    process_hashes_top = [p.get("hash") for p in process_top] if isinstance(process_top, list) else []
    seal_ph = seal.get("process_hashes")
    if process_count > 0:
        if seal_ph != process_hashes_top:
            return False
    else:
        if seal_ph is not None:
            return False
    return True


# --------------------------------------------------------------------------
# Process Trust — deterministic, integer-only, offline. Mirrors
# truetake_verify::compute_trust byte-for-byte (the three-way gate proves bit
# equality). NOT an AI/human verdict: it reports the thickness + internal
# consistency of the recorded process. `ts` is self-reported (chain-internal
# coherence, not wall-clock proof).
# --------------------------------------------------------------------------

TRUST_VERSION = "trust-0"


def _pct(num, den):
    return 100 if den == 0 else (num * 100) // den


def compute_trust(cert) -> dict:
    receipts = cert.get("receipts") or []
    process_top = cert.get("process") or []
    n_process = len(process_top)
    basis = "chain" if n_process > 0 else "none"

    def has_receipt(kind, idx_key, idx):
        for r in receipts:
            if r.get("kind") != kind:
                continue
            if not idx_key:
                return True
            b = r.get("binds")
            if isinstance(b, dict) and b.get(idx_key) == idx:
                return True
        return False

    sth = cert.get("sth") if isinstance(cert.get("sth"), dict) else {}
    order = [e for e in (sth.get("process_order") or []) if isinstance(e, str)]

    # 核1 integrity: every timeline entry resolves to a signed receipt.
    resolved = 0
    for e in order:
        if e == "seal":
            ok = has_receipt("seal", "", 0)
        elif e == "ingest:work":
            ok = has_receipt("work", "", 0)
        elif e.startswith("ingest:material:") and e[len("ingest:material:"):].isdigit():
            ok = has_receipt("material", "material_index", int(e[len("ingest:material:"):]))
        elif e.startswith("process:") and e[len("process:"):].isdigit():
            ok = has_receipt("process", "process_index", int(e[len("process:"):]))
        else:
            ok = False
        if ok:
            resolved += 1
    integrity_pct = _pct(resolved, len(order))

    # 核2 disclosure: each final-bound input has its token in the timeline.
    seal_binds = {}
    for r in receipts:
        if r.get("kind") == "seal" and isinstance(r.get("binds"), dict):
            seal_binds = r["binds"]
            break
    n_mat = len(seal_binds.get("material_hashes") or [])
    n_proc_h = len(seal_binds.get("process_hashes") or [])
    order_set = set(order)
    disclosed = 1 if "ingest:work" in order_set else 0
    disclose_total = 1
    for i in range(n_mat):
        disclose_total += 1
        if ("ingest:material:%d" % i) in order_set:
            disclosed += 1
    for j in range(n_proc_h):
        disclose_total += 1
        if ("process:%d" % j) in order_set:
            disclosed += 1
    disclosure_pct = _pct(disclosed, disclose_total)

    # 補助1 time_code: monotonic self-reported ts + seal after last.
    if n_process < 2:
        time_code = 0
    else:
        ts = [p.get("ts") if isinstance(p.get("ts"), int) and not isinstance(p.get("ts"), bool) else -1
              for p in process_top]
        mono = all(ts[k] < ts[k + 1] for k in range(len(ts) - 1)) and all(t >= 0 for t in ts)
        seal_ts = sth.get("ts") if isinstance(sth.get("ts"), int) and not isinstance(sth.get("ts"), bool) else -1
        seal_after = seal_ts >= ts[-1]
        time_code = 2 if (mono and seal_after) else 1

    # 補助2 declaration_pct: items sharing a hash must share ai (+origin/type).
    materials = cert.get("materials") or []

    def group_consistency(items, attrs):
        buckets = {}
        for it in items:
            h = it.get("hash") if isinstance(it, dict) else None
            if isinstance(h, str):
                buckets.setdefault(h, []).append(it)
        g = c = 0
        for _h, v in buckets.items():
            if len(v) >= 2:
                g += 1
                first = v[0]
                if all(all(it.get(a) == first.get(a) for a in attrs) for it in v):
                    c += 1
        return g, c

    g1, c1 = group_consistency(materials, ("ai", "origin"))
    g2, c2 = group_consistency(process_top, ("ai", "type"))
    declaration_pct = _pct(c1 + c2, g1 + g2)

    fully = integrity_pct == 100 and disclosure_pct == 100 and declaration_pct == 100
    if n_process == 0:
        label = "thin"
    elif fully and n_process >= 3 and time_code == 2:
        label = "thick"
    elif fully and n_process >= 1:
        label = "standard"
    else:
        label = "thin"

    return {
        "version": TRUST_VERSION,
        "label": label,
        "basis": basis,
        "n_process": n_process,
        "integrity_pct": integrity_pct,
        "disclosure_pct": disclosure_pct,
        "time_code": time_code,
        "declaration_pct": declaration_pct,
    }


def _check_process_trust(cert) -> bool:
    pt = cert.get("process_trust")
    if not isinstance(pt, dict) or "sig" not in pt:
        return False
    sig = pt.get("sig")
    stored_core = {k: v for k, v in pt.items() if k != "sig"}
    if stored_core != compute_trust(cert):
        return False
    log_pubkey = (cert.get("sth") or {}).get("log_pubkey")
    digest = _sha256(scs.to_scs_bytes(stored_core))
    return _verify_ed25519(log_pubkey, digest, sig)


# --------------------------------------------------------------------------
# Human Signature — a SEPARATE observation from process_trust, alongside it.
# Mirrors truetake_verify::compute_signature byte-for-byte (the three-way gate
# proves bit equality). NOT an AI/human verdict: it reports, integer-only and
# offline, how far the recorded process STRUCTURE aligns with human iterative
# creation. axis X = interruption/abandonment traces (discards, redos, ts-gap
# dispersion); axis Y = technical-layering gradient (craft op-count rising).
# craft/ts/drafts are the author's self-report (a pattern in the chain, not proof
# of real editing). insufficient when no process / no craft is recorded.
# --------------------------------------------------------------------------

SIGNATURE_VERSION = "humansig-0"


def _as_int(v):
    """A genuine JSON integer (not bool), else None — matches serde as_u64/as_i64."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def compute_signature(cert) -> dict:
    process_top = cert.get("process") or []
    if not isinstance(process_top, list):
        process_top = []
    n_process = len(process_top)

    # axis Y inputs: craft op-count sequence over craft-bearing steps.
    op_count_seq = []
    n_craft = 0
    y_op_total = 0
    y_layers_max = 0
    for p in process_top:
        c = p.get("craft") if isinstance(p, dict) else None
        if isinstance(c, dict):
            n_craft += 1
            oc = _as_int(c.get("op_count"))
            oc = oc if (oc is not None and oc >= 0) else 0
            y_op_total += oc
            op_count_seq.append(oc)
            la = _as_int(c.get("layers"))
            if la is not None and la > y_layers_max:
                y_layers_max = la
    if len(op_count_seq) < 2:
        y_ascending_pct, y_code = 0, 0
    else:
        pairs = len(op_count_seq) - 1
        inc = sum(1 for k in range(pairs) if op_count_seq[k + 1] > op_count_seq[k])
        y_ascending_pct = _pct(inc, pairs)
        if n_craft >= 3 and y_ascending_pct == 100:
            y_code = 2
        elif inc >= 1:
            y_code = 1
        else:
            y_code = 0

    # axis X inputs: discards, redos, ts-gap dispersion.
    types = [p.get("type") if isinstance(p, dict) and isinstance(p.get("type"), str) else "" for p in process_top]
    x_discards = sum(1 for t in types if t == "draft")
    x_redos = sum(1 for k in range(len(types) - 1) if types[k] and types[k] == types[k + 1])
    ts = []
    for p in process_top:
        t = _as_int(p.get("ts")) if isinstance(p, dict) else None
        ts.append(t if t is not None else -1)
    if len(ts) < 2 or any(t < 0 for t in ts):
        x_ts_dispersion = 0
    else:
        gaps = [ts[k + 1] - ts[k] for k in range(len(ts) - 1)]
        if any(g <= 0 for g in gaps):
            x_ts_dispersion = 0
        else:
            n = len(gaps)
            mean = sum(gaps) // n  # integer floor (positive)
            if mean == 0:
                x_ts_dispersion = 0
            else:
                mad = sum(abs(g - mean) for g in gaps) // n
                x_ts_dispersion = (mad * 100) // mean
    if x_discards >= 2 and x_redos >= 1 and x_ts_dispersion >= 20:
        x_code = 2
    elif x_discards >= 1 or x_redos >= 1:
        x_code = 1
    else:
        x_code = 0

    # integration.
    if n_craft >= 1:
        basis = "chain+craft"
    elif n_process >= 1:
        basis = "chain"
    else:
        basis = "none"
    if n_process == 0 or n_craft == 0:
        label = "insufficient"
    elif x_code == 2 and y_code == 2:
        label = "high"
    elif x_code + y_code <= 1:
        label = "low"
    else:
        label = "standard"
    forge_cost = 0 if label == "insufficient" else (n_process + y_op_total)

    return {
        "version": SIGNATURE_VERSION,
        "label": label,
        "basis": basis,
        "n_process": n_process,
        "n_craft": n_craft,
        "x_discards": x_discards,
        "x_redos": x_redos,
        "x_ts_dispersion": x_ts_dispersion,
        "x_code": x_code,
        "y_op_total": y_op_total,
        "y_layers_max": y_layers_max,
        "y_ascending_pct": y_ascending_pct,
        "y_code": y_code,
        "forge_cost": forge_cost,
    }


def _check_human_signature(cert) -> bool:
    hs = cert.get("human_signature")
    if not isinstance(hs, dict) or "sig" not in hs:
        return False
    sig = hs.get("sig")
    stored_core = {k: v for k, v in hs.items() if k != "sig"}
    if stored_core != compute_signature(cert):
        return False
    log_pubkey = (cert.get("sth") or {}).get("log_pubkey")
    digest = _sha256(scs.to_scs_bytes(stored_core))
    return _verify_ed25519(log_pubkey, digest, sig)


def verify_cert(path) -> tuple:
    try:
        with open(path, "rb") as f:
            cert = json.load(f)
    except Exception as e:
        return EXIT_ERROR, f"cannot read/parse {path}: {e}"
    return verify_cert_obj(cert)


def main(argv) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: python3 -m pyverify.truetake <cert.json>", file=sys.stderr)
        return 2
    code, detail = verify_cert(argv[1])
    label = "CONFIRMED" if code == 0 else ("ERROR" if code == 1 else "FAILED")
    print(f"pyverify.truetake: {label} (exit {code}) — {detail}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
