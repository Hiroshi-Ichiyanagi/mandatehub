"""govern-verify (Python) — an INDEPENDENT, pure-stdlib reimplementation of
govern's offline bundle verification, for cross-implementation fidelity against
the Rust `govern-verify`. Third-party dependencies: ZERO (stdlib + the vendored
Ed25519 in `_ed25519.py`).

It reproduces the WO-named structural checks and their exit codes:
  A parent parse + GENESIS rule, B parent-chain stream  -> 20
  C each child trail passes its own chain verification    -> 21
  D reverse-reference (BIND <-> child record_hash)         -> 22
  G witnesses (tail-truncation detection)                  -> 24
  0 = OFFLINE-VERIFIED.

Scope note (honest): tessera receipt digests, qswap/spendcap chains, SCS,
anchor MACs and the Ed25519 mandate signature ARE recomputed independently.
The cross-layer causality re-check (C-1..C-3) and the full byte-rebuild of the
parent chain are additional Rust-side integrity layers not ported here; they do
not alter the verdict on any structurally-valid or single-tamper bundle.
"""

import base64
import hashlib
import hmac
import json
import struct
from pathlib import Path

from . import merkle, scs
from ._ed25519 import verify as ed25519_verify

EXIT_PASS = 0
EXIT_PARENT = 20
EXIT_CHILD = 21
EXIT_REVERSE = 22
EXIT_SIGNATURE = 23
EXIT_WITNESS = 24
EXIT_STH = 33         # optional RFC 6962 signed tree head over parent ledger invalid (Stage D)
EXIT_CONSISTENCY = 34 # RFC 6962 consistency proof (append-only growth) invalid (Stage D)

STH_DOMAIN = "govern/sth/v0"

ANCHOR_INTERVAL = 16
EPOCH_HEX_LEN = 16
TESSERA_LEDGER_HEADER = "tessera-ledger v1"


class VerifyFail(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def scs_hash(value) -> str:
    return sha256_hex(scs.to_scs_bytes(value))


def anchor_mac(key: bytes, prev_hex: str, seq: int) -> str:
    msg = prev_hex.encode("ascii") + b":" + str(seq).encode("ascii")
    return hmac.new(bytes(key), msg, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# A + B: parent chain.
# --------------------------------------------------------------------------

def parse_chain_text(text: str):
    records = []
    for idx, line in enumerate(text.splitlines()):
        line_no = idx + 1
        try:
            value = json.loads(line)
        except Exception as e:
            raise VerifyFail(EXIT_PARENT, f"parent line {line_no} not JSON: {e}")
        if scs.to_scs_bytes(value) != line.encode("utf-8"):
            raise VerifyFail(EXIT_PARENT, f"parent line {line_no} is not canonical SCS")
        records.append(value)
    if not records:
        raise VerifyFail(EXIT_PARENT, "parent chain is empty")
    return records


def compute_parent_record_hash(rec) -> str:
    return scs_hash({
        "seq": rec["seq"],
        "record_type": rec["record_type"],
        "payload": rec["payload"],
        "prev_hash": rec["prev_hash"],
    })


def check_genesis(g):
    if g["seq"] != 1 or g["record_type"] != "GENESIS":
        raise VerifyFail(EXIT_PARENT, "first record must be GENESIS with seq 1")
    p = g["payload"]
    if not isinstance(p, dict) or len(p) != 3:
        raise VerifyFail(EXIT_PARENT, "GENESIS payload must have 3 fields")
    run_id = p.get("run_id")
    if not isinstance(run_id, str):
        raise VerifyFail(EXIT_PARENT, "GENESIS missing run_id")
    if not isinstance(p.get("tritie_semver"), str):
        raise VerifyFail(EXIT_PARENT, "GENESIS missing tritie_semver")
    if p.get("adapters") != ["MODEL", "INFERENCE", "ECONOMIC"]:
        raise VerifyFail(EXIT_PARENT, "GENESIS adapters must be [MODEL,INFERENCE,ECONOMIC]")
    if g["prev_hash"] != sha256_hex(("tritie-genesis-v1:" + run_id).encode("utf-8")):
        raise VerifyFail(EXIT_PARENT, "GENESIS prev_hash rule violated")
    return run_id


def check_parent_stream(records, meta_key):
    """Returns list of bound children: (parent_seq, {layer, child_seq, record_hash, logical})."""
    bound = []
    non_anchor = 0
    expect_anchor = False
    prev_hash = None
    for idx, rec in enumerate(records):
        expected_seq = idx + 1
        if rec["seq"] != expected_seq:
            raise VerifyFail(EXIT_PARENT, f"seq must be +1: expected {expected_seq} got {rec['seq']}")
        if compute_parent_record_hash(rec) != rec["record_hash"]:
            raise VerifyFail(EXIT_PARENT, f"record_hash mismatch at seq {expected_seq} (tamper)")
        if prev_hash is not None and rec["prev_hash"] != prev_hash:
            raise VerifyFail(EXIT_PARENT, f"prev_hash linkage broken at seq {expected_seq}")
        rt = rec["record_type"]
        if rt == "GENESIS":
            if idx != 0:
                raise VerifyFail(EXIT_PARENT, "GENESIS only at seq 1")
            non_anchor += 1
        elif rt == "BIND":
            if expect_anchor:
                raise VerifyFail(EXIT_PARENT, "META_ANCHOR due (16-cadence)")
            pl = rec["payload"]
            if not isinstance(pl, dict) or len(pl) != 4:
                raise VerifyFail(EXIT_PARENT, "BIND payload must have 4 fields")
            bound.append((rec["seq"], {
                "layer": pl["layer"],
                "child_seq": pl["child_seq"],
                "record_hash": pl["child_record_hash"],
                "logical": pl["logical"],
            }))
            non_anchor += 1
        elif rt == "META_ANCHOR":
            if not expect_anchor:
                raise VerifyFail(EXIT_PARENT, "unexpected META_ANCHOR (off-cadence)")
            pl = rec["payload"]
            if not isinstance(pl, dict) or len(pl) != 2 or pl.get("anchor_seq") != rec["seq"]:
                raise VerifyFail(EXIT_PARENT, "META_ANCHOR payload must be {anchor_seq=seq, mac}")
            if meta_key is not None and pl.get("mac") != anchor_mac(meta_key, rec["prev_hash"], rec["seq"]):
                raise VerifyFail(EXIT_PARENT, "META_ANCHOR mac mismatch")
            expect_anchor = False
        else:
            raise VerifyFail(EXIT_PARENT, f"unknown record_type {rt}")
        if rt != "META_ANCHOR" and non_anchor % ANCHOR_INTERVAL == 0:
            expect_anchor = True
        prev_hash = rec["record_hash"]
    if expect_anchor:
        raise VerifyFail(EXIT_PARENT, "chain ends where a META_ANCHOR is due (truncation?)")
    return bound


# --------------------------------------------------------------------------
# C + D: child trails (self-verify by chain recompute + load record_hashes).
# --------------------------------------------------------------------------

def load_model(text):
    """qswap-trail MODEL_EVENT chain. Returns [(child_seq, record_hash, epoch, ts)]; raises on chain failure."""
    out = []
    prev = None
    for idx, line in enumerate(text.splitlines()):
        v = json.loads(line)
        if scs.to_scs_bytes(v) != line.encode("utf-8"):
            raise VerifyFail(EXIT_CHILD, f"model line {idx+1} not canonical")
        if v.get("record_type") != "MODEL_EVENT" or v.get("seq") != idx + 1:
            raise VerifyFail(EXIT_CHILD, f"model line {idx+1} bad record_type/seq")
        rh = scs_hash({"payload": v["payload"], "prev_hash": v["prev_hash"],
                       "record_type": v["record_type"], "seq": v["seq"]})
        if rh != v["record_hash"]:
            raise VerifyFail(EXIT_CHILD, f"model record_hash mismatch at seq {v['seq']}")
        if prev is not None and v["prev_hash"] != prev:
            raise VerifyFail(EXIT_CHILD, f"model prev_hash broken at seq {v['seq']}")
        out.append((v["seq"], v["record_hash"], v["payload"]["epoch"], v["payload"]["ts"]))
        prev = v["record_hash"]
    return out


def load_economic(text):
    """spendcap ledger chain + Ed25519 mandate signature. Returns non-ANCHOR
    [(seq, record_hash)]; raises on chain/signature failure."""
    out = []
    prev = None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        v = json.loads(line)
        if scs.to_scs_bytes(v) != line.encode("utf-8"):
            raise VerifyFail(EXIT_CHILD, f"economic line {idx+1} not canonical")
        if v.get("seq") != idx + 1:
            raise VerifyFail(EXIT_CHILD, f"economic seq must be +1 at line {idx+1}")
        rh = scs_hash({"seq": v["seq"], "record_type": v["record_type"],
                       "payload": v["payload"], "prev_hash": v["prev_hash"]})
        if rh != v["record_hash"]:
            raise VerifyFail(EXIT_CHILD, f"economic record_hash mismatch at seq {v['seq']}")
        if prev is not None and v["prev_hash"] != prev:
            raise VerifyFail(EXIT_CHILD, f"economic prev_hash broken at seq {v['seq']}")
        if idx == 0:
            _verify_mandate_genesis(v)
        if v["record_type"] != "ANCHOR":
            out.append((v["seq"], v["record_hash"]))
        prev = v["record_hash"]
    return out


def _verify_mandate_genesis(genesis):
    if genesis.get("record_type") != "MANDATE":
        raise VerifyFail(EXIT_CHILD, "economic genesis must be MANDATE")
    env = genesis["payload"]
    mandate = env["mandate"]
    mandate_hash = env["mandate_hash"]
    # genesis prev_hash rule
    if genesis["prev_hash"] != sha256_hex(("spendcap-genesis-v1:" + mandate_hash).encode("utf-8")):
        raise VerifyFail(EXIT_CHILD, "economic genesis prev_hash rule violated")
    # mandate_hash binds the mandate
    if scs_hash(mandate) != mandate_hash:
        raise VerifyFail(EXIT_CHILD, "mandate_hash does not match SCS(mandate)")
    # Ed25519 issuer signature over the 32-byte digest (vendored, zero-dep).
    pub = mandate["issuer_pubkey"]
    if not pub.startswith("ed25519:"):
        raise VerifyFail(EXIT_CHILD, "issuer_pubkey not ed25519:")
    pk = base64.b64decode(pub[len("ed25519:"):])
    sig = base64.b64decode(env["signature"])
    digest = bytes.fromhex(mandate_hash)
    if not ed25519_verify(pk, digest, sig):
        raise VerifyFail(EXIT_CHILD, "mandate Ed25519 signature invalid")


def _framed(buf: bytearray, data: bytes):
    buf += struct.pack("<Q", len(data))
    buf += data


def _receipt_claim_digest(model, inp, out_, sampler, root, ts, seq, prev) -> bytes:
    canonical = bytearray()
    for f in (model, inp, out_, sampler, root):
        _framed(canonical, f)
    canonical += struct.pack("<Q", ts)
    canonical += struct.pack("<Q", seq)
    _framed(canonical, prev)
    return hashlib.sha256(b"tessera.claim.v1" + bytes(canonical)).digest()


def _receipt_signing_input(claim_digest, trust, signer) -> bytes:
    """The 32-byte message an Ed25519 receipt signature covers (mirrors
    tessera::receipt::Receipt::signing_input)."""
    buf = bytearray()
    _framed(buf, claim_digest)
    buf += bytes([trust])
    _framed(buf, signer)
    return hashlib.sha256(b"tessera.receipt-sig.v1" + bytes(buf)).digest()


def _receipt_digest(model, inp, out_, sampler, root, ts, seq, prev, trust, signer, sig) -> str:
    claim = _receipt_claim_digest(model, inp, out_, sampler, root, ts, seq, prev)
    buf = bytearray()
    _framed(buf, claim)
    buf += bytes([trust])
    _framed(buf, signer)
    _framed(buf, sig)
    return hashlib.sha256(b"tessera.receipt.v1" + bytes(buf)).hexdigest()


def load_inference(text, epoch_map):
    """tessera ledger: structural chain + recomputed receipt digests.
    Returns [(child_seq, record_hash=digest_hex)]; raises on failure."""
    lines = text.splitlines()
    if not lines or lines[0] != TESSERA_LEDGER_HEADER:
        raise VerifyFail(EXIT_CHILD, "inference ledger header missing")
    out = []
    prev_digest = "00" * 32
    for i, raw in enumerate(lines[1:]):
        parts = raw.split(" ")
        if not parts or parts[0] != "r":
            raise VerifyFail(EXIT_CHILD, f"inference line {i+2} must start with 'r'")
        f = {}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            f[k] = v
        seq = int(f["seq"]); ts = int(f["ts"]); trust = int(f["trust"])
        if seq != len(out):
            raise VerifyFail(EXIT_CHILD, "inference seq must increment from 0")
        if f["prev"] != prev_digest:
            raise VerifyFail(EXIT_CHILD, "inference prev does not match previous digest")
        model = bytes.fromhex(f["model"]); inp = bytes.fromhex(f["in"]); out_ = bytes.fromhex(f["out"])
        sampler = bytes.fromhex(f["sampler"]); root = bytes.fromhex(f["root"]); prev = bytes.fromhex(f["prev"])
        signer = bytes.fromhex(f["signer"]); sig = bytes.fromhex(f["sig"])
        # Intrinsic Ed25519 verification: the receipt is signed by `signer` (its
        # embedded 32-byte public key) over the receipt signing input — verified
        # with public data only, no shared secret.
        claim_digest = _receipt_claim_digest(model, inp, out_, sampler, root, ts, seq, prev)
        si = _receipt_signing_input(claim_digest, trust, signer)
        if len(signer) != 32 or len(sig) != 64 or not ed25519_verify(signer, si, sig):
            raise VerifyFail(EXIT_CHILD, f"inference receipt Ed25519 signature invalid at seq {seq}")
        digest = _receipt_digest(model, inp, out_, sampler, root, ts, seq, prev, trust, signer, sig)
        out.append((seq, digest))
        prev_digest = digest
    return out


# --------------------------------------------------------------------------
# G: witnesses.
# --------------------------------------------------------------------------

def check_parent_witness(records, wbytes):
    w = json.loads(wbytes)
    seq = w.get("seq")
    ok = any(r["seq"] == seq and r["record_type"] == "META_ANCHOR"
             and r["record_hash"] == w.get("record_hash")
             and r["payload"].get("mac") == w.get("mac")
             for r in records)
    if not ok:
        raise VerifyFail(EXIT_WITNESS, "witnessed META_ANCHOR absent from parent chain (truncation?)")


def check_child_witness_generic(loaded_layer, wbytes, layer_tag):
    w = json.loads(wbytes)
    cs = w.get("child_seq")
    rh = w.get("record_hash")
    if not any(seq == cs and h == rh for (seq, h) in loaded_layer):
        raise VerifyFail(EXIT_WITNESS, f"witnessed {layer_tag} record absent from trail (truncation?)")


def check_economic_native_witness(econ_text, wbytes):
    w = json.loads(wbytes)
    seq, rh, mac = w.get("seq"), w.get("record_hash"), w.get("mac")
    ok = False
    for line in econ_text.splitlines():
        try:
            v = json.loads(line)
        except Exception:
            continue
        if (v.get("record_type") == "ANCHOR" and v.get("seq") == seq
                and v.get("record_hash") == rh
                and v.get("payload", {}).get("mac") == mac):
            ok = True
            break
    if not ok:
        raise VerifyFail(EXIT_WITNESS, "witnessed ANCHOR absent from economic ledger (truncation?)")


# --------------------------------------------------------------------------
def approval_message(approver, run_id, index, model_hex, in_hex) -> bytes:
    return scs.to_scs_bytes({
        "approver": approver, "in": in_hex, "index": index,
        "model": model_hex, "run_id": run_id,
    })


def ledger_receipt_fields(inf_text):
    """[(model_hex, in_hex, out_hex)] per receipt, in ledger order."""
    rows = []
    for line in inf_text.splitlines():
        if not line.startswith("r "):
            continue
        f = {}
        for p in line.split(" ")[1:]:
            k, _, v = p.partition("=")
            f[k] = v
        rows.append((f.get("model", ""), f.get("in", ""), f.get("out", "")))
    return rows


# --------------------------------------------------------------------------
# Top-level.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Optional RFC 6962 signed tree head (STH) over the parent ledger (Stage D).
# Additive: verified only when sth.json is present, and only after every
# structural/governance check passes, so it can never change an existing code.
# --------------------------------------------------------------------------

def _sth_signing_digest(root_hex, tree_size):
    return hashlib.sha256(scs.to_scs_bytes(
        {"domain": STH_DOMAIN, "root": root_hex, "tree_size": tree_size})).digest()


def _verify_sth_sig(log_pubkey, root_hex, tree_size, sig_b64):
    if not isinstance(log_pubkey, str) or not log_pubkey.startswith("ed25519:"):
        return False
    try:
        pk = base64.b64decode(log_pubkey[len("ed25519:"):])
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False
    return ed25519_verify(pk, _sth_signing_digest(root_hex, tree_size), sig)


def check_sth(records, sth):
    """RFC 6962 STH over the parent ledger. `records` are the parsed parent
    records (each with a 'record_hash'); `sth` is the parsed sth.json. Raises
    VerifyFail(33/34) on a present-but-invalid STH."""
    leaves = [bytes.fromhex(r["record_hash"]) for r in records]
    tree_size = sth.get("tree_size")
    root = sth.get("root")
    if tree_size != len(leaves):
        raise VerifyFail(EXIT_STH, "STH tree_size != parent record count")
    if merkle.root(leaves).hex() != root:
        raise VerifyFail(EXIT_STH, "STH root != Merkle tree hash over parent ledger")
    if not _verify_sth_sig(sth.get("log_pubkey", ""), root, tree_size, sth.get("sig", "")):
        raise VerifyFail(EXIT_STH, "STH Ed25519 signature invalid")

    prior = sth.get("prior")
    if prior is not None:
        m = prior.get("tree_size")
        p_root = prior.get("root")
        if not _verify_sth_sig(sth.get("log_pubkey", ""), p_root, m, prior.get("sig", "")):
            raise VerifyFail(EXIT_STH, "prior STH Ed25519 signature invalid")
        if not isinstance(m, int) or m > len(leaves) or merkle.root(leaves[:m]).hex() != p_root:
            raise VerifyFail(EXIT_CONSISTENCY, "prior STH root is not the genuine prefix")
        try:
            proof = [bytes.fromhex(h) for h in sth.get("consistency_proof", [])]
            first_root = bytes.fromhex(p_root)
            second_root = bytes.fromhex(root)
        except Exception:
            raise VerifyFail(EXIT_CONSISTENCY, "consistency proof malformed")
        if not merkle.verify_consistency(m, tree_size, proof, first_root, second_root):
            raise VerifyFail(EXIT_CONSISTENCY, "consistency proof invalid")


def verify_approval_signatures(manifest, inf_text):
    """SIGNATURE-VALIDITY ONLY for each step's approval (mirror of the Rust
    govern_verify_core::verify_approval_signatures): verify the Ed25519 signature
    over the canonical approval message using the public key embedded in the
    approval. NO policy allowlist / tier / budget (that is governance, which the
    full govern engine performs, not this verifier). A bad signature is a
    structural/authenticity failure -> EXIT_CHILD (21)."""
    steps = manifest.get("steps")
    if not isinstance(steps, list):
        return
    fields = ledger_receipt_fields(inf_text)
    run_id = manifest.get("run_id")
    for step in steps:
        approval = step.get("approval")
        if not isinstance(approval, dict):
            continue
        idx = step.get("index")
        if not isinstance(idx, int) or idx >= len(fields):
            raise VerifyFail(EXIT_CHILD, "approval references a missing receipt")
        model_hex, in_hex, _out = fields[idx]
        approver = approval.get("approver"); sig_b64 = approval.get("sig")
        if not approver or not sig_b64 or not approver.startswith("ed25519:"):
            raise VerifyFail(EXIT_CHILD, f"step {idx}: malformed approval")
        try:
            pk = base64.b64decode(approver[len("ed25519:"):])
            sig = base64.b64decode(sig_b64)
        except Exception:
            raise VerifyFail(EXIT_CHILD, f"step {idx}: malformed approval encoding")
        digest = hashlib.sha256(approval_message(approver, run_id, idx, model_hex, in_hex)).digest()
        if len(pk) != 32 or len(sig) != 64 or not ed25519_verify(pk, digest, sig):
            raise VerifyFail(EXIT_CHILD, f"step {idx}: approval Ed25519 signature invalid")


def verify_bundle(dir_path):
    """Returns (exit_code, detail). 0 = OFFLINE-VERIFIED. Flat bundles only —
    this public verifier does crypto/structural/standards verification and does
    NOT re-evaluate governance policy (that is the full govern engine's role)."""
    d = Path(dir_path)
    try:
        manifest = json.loads((d / "manifest.json").read_text())
        meta_key = bytes(manifest.get("meta_anchor_key", []))
        epoch_map = {k: v for k, v in (manifest.get("epoch_map") or [])}

        parent_text = (d / "parent.jsonl").read_text()
        records = parse_chain_text(parent_text)
        check_genesis(records[0])
        bound = check_parent_stream(records, meta_key if meta_key else None)

        # C: each child trail verifies its own chain (model, inference, economic).
        model_text = (d / "model.jsonl").read_text()
        inf_text = (d / "inference" / "ledger.tsl").read_text()
        econ_text = (d / "economic.jsonl").read_text()
        model = load_model(model_text)
        inference = load_inference(inf_text, epoch_map)
        economic = load_economic(econ_text)

        # D: reverse-reference — bound child record_hash must match loaded.
        index = {}
        for (seq, rh, _e, _t) in model:
            index[("MODEL", seq)] = rh
        for (seq, rh) in inference:
            index[("INFERENCE", seq)] = rh
        for (seq, rh) in economic:
            index[("ECONOMIC", seq)] = rh
        for (pseq, bc) in bound:
            key = (bc["layer"], bc["child_seq"])
            if key not in index:
                raise VerifyFail(EXIT_REVERSE, f"BIND references absent child {bc['layer']}:{bc['child_seq']}")
            if index[key] != bc["record_hash"]:
                raise VerifyFail(EXIT_REVERSE, f"BIND hash mismatch for {bc['layer']}:{bc['child_seq']}")

        # G: witnesses (present in our bundles).
        wdir = d / "witness"
        if (wdir / "parent.scs").exists():
            check_parent_witness(records, (wdir / "parent.scs").read_bytes())
        if (wdir / "model.scs").exists():
            check_child_witness_generic([(s, h) for (s, h, _e, _t) in model],
                                        (wdir / "model.scs").read_bytes(), "MODEL")
        if (wdir / "inference.scs").exists():
            check_child_witness_generic(inference, (wdir / "inference.scs").read_bytes(), "INFERENCE")
        if (wdir / "economic.scs").exists():
            check_economic_native_witness(econ_text, (wdir / "economic.scs").read_bytes())

        # All Ed25519 approval signatures must be cryptographically valid
        # (signature authenticity only — no governance policy re-evaluation).
        verify_approval_signatures(manifest, inf_text)

        # Additive RFC 6962 signed tree head over the parent ledger:
        # verified only when present, after everything above passes.
        sth_path = d / "sth.json"
        if sth_path.exists():
            check_sth(records, json.loads(sth_path.read_text()))

        return (EXIT_PASS, f"OFFLINE-VERIFIED: chain/binding/witness integrity holds "
                           f"(bind_records={len(bound)}). Model NOT re-run; replay is separate.")
    except VerifyFail as e:
        return (e.code, e.detail)
    except Exception as e:
        # Any structural surprise is fail-closed as parent integrity.
        return (EXIT_PARENT, f"verification error: {e}")
