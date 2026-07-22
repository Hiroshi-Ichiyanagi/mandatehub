"""
genesis-keystone :: anchor hardening for the audit chain (Tier P, disclosure-safe)

Decision D-4. The audit chain's head hash is a commitment to the whole history,
so a stored ``(head, length)`` anchor lets ``AuditLog.verify(expected_head=...)``
detect truncation / deletion / reorder / edit (B2/D-3). But if that anchor lives
in the same trust boundary as the JSONL, an attacker who can rewrite the log can
also recompute and rewrite the anchor — "tamper-evident" only on paper.

This module raises the bar: the anchor is **HMAC-signed** with a secret key kept
in a *different* trust boundary (env var or a chmod-600 key file). To pass
verification after truncating the log, an attacker must now forge a valid HMAC of
the new head — infeasible without the key. Attack cost: "rewrite a file" ->
"steal the key".

Threat model & residual (honest):
  * Defends against: an attacker with read/write/truncate on the audit JSONL who
    does NOT hold the signing key.
  * Does NOT defend against: theft of the signing key, or a root-level attacker
    who controls the whole machine (env, key file, and process memory). Pure
    local stdlib cannot make tampering impossible against root — only detectable
    while the key stays secret.
  * HMAC is symmetric: the verifier holds the same key. "Anyone can verify"
    (asymmetric, e.g. ed25519) would need a non-stdlib dependency and is out of
    scope here; see the report's external-witness proposal for stronger options.

Key provisioning (default, safest-local):
  * env ``KEYSTONE_ANCHOR_HMAC_KEY`` (raw secret), OR
  * env ``KEYSTONE_ANCHOR_KEY_FILE`` (path to a chmod-600 file holding the key).
  * If neither is set, ``load_key()`` returns ``None`` -> callers fall back to an
    UNSIGNED anchor (D-3 level only; forgeable) — backward compatible.
The key value is never logged, returned in reprs, or written by this module.

stdlib only (hmac, hashlib, os).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class SignedAnchor:
    """A capture of the chain head, its length, and an optional HMAC signature.

    ``signature is None`` means it was produced without a key (D-3 level: catches
    accidental/again-recomputable truncation, but is forgeable by an attacker).
    ``key_id`` (D-7) is a NON-secret label of the signing key so a verifier can
    select the right key after rotation. It is never the key itself.
    """

    head: str
    length: int
    signature: Optional[str] = None
    key_id: Optional[str] = None


def key_id(key: bytes) -> str:
    """Non-secret short identifier for a key (first 12 hex of its sha256).

    Used to label which key signed an anchor (rotation). Deterministic, and not
    reversible to the key. Never log the key itself.
    """
    return hashlib.sha256(key).hexdigest()[:12]


def _mac(head: str, length: int, key: bytes) -> str:
    msg = f"{head}:{length}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def load_key() -> Optional[bytes]:
    """Read the signing key from env / key-file, or ``None`` if unconfigured.

    Order: ``KEYSTONE_ANCHOR_HMAC_KEY`` (raw) then ``KEYSTONE_ANCHOR_KEY_FILE``
    (path). The key value is never logged. Returns raw bytes or ``None``.
    """
    raw = os.environ.get("KEYSTONE_ANCHOR_HMAC_KEY")
    if raw:
        return raw.encode("utf-8")
    path = os.environ.get("KEYSTONE_ANCHOR_KEY_FILE")
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()  # raw bytes — never strip a binary key
        return data or None
    return None


def anchor_for(head: str, length: int, key: Optional[bytes]) -> SignedAnchor:
    """Build a (optionally HMAC-signed) anchor from a head+length directly.

    Useful for checkpointing when you have the head/length but not a live log.
    With ``key=None`` the anchor is unsigned (D-3 level); with a key it is signed
    and tagged with the key's ``key_id`` (D-7) for rotation-aware verification.
    """
    sig = _mac(head, length, key) if key else None
    kid = key_id(key) if key else None
    return SignedAnchor(head=head, length=length, signature=sig, key_id=kid)


def make_signed_anchor(log, key: Optional[bytes]) -> SignedAnchor:
    """Capture ``(head, length)`` of ``log`` and HMAC-sign it if a key is given.

    With ``key=None`` the anchor is unsigned (D-3 level). Store the returned
    anchor in a *different* trust boundary than the audit JSONL.
    """
    return anchor_for(log.head, len(log.records()), key)


def verify_against_signed_anchor(
    log, anchor: SignedAnchor, key: Optional[bytes],
) -> bool:
    """Verify ``log`` against a stored ``anchor``.

    1. The chain must be internally intact AND match ``anchor.head``/``length``
       (truncation/edit/reorder caught — D-3).
    2. If a ``key`` is supplied (hardened mode), the anchor MUST carry a valid
       HMAC under that key. An unsigned anchor, or one signed with the wrong key
       (e.g. an attacker who truncated the log and forged a fresh anchor without
       the secret), is rejected. With ``key=None`` (unconfigured), step 2 is
       skipped — D-3 level only (residual: anchor is forgeable).
    """
    if not log.verify(expected_head=anchor.head, expected_len=anchor.length):
        return False
    if key is not None:
        if not anchor.signature:
            return False  # hardened mode requires a signature
        expected = _mac(anchor.head, anchor.length, key)
        return hmac.compare_digest(expected, anchor.signature)
    return True


def verify_with_keyring(
    log, anchor: SignedAnchor, keyring: Mapping[str, bytes],
) -> bool:
    """Rotation-aware verify (D-7): select the key by ``anchor.key_id``.

    Looks up the key that signed this anchor in ``keyring`` (``key_id -> key``)
    and verifies under it. If the anchor is unsigned or its ``key_id`` is not in
    the keyring, returns ``False`` (fail-closed — an old anchor needs its old key
    retained in the keyring). Lets you rotate the active key while still
    verifying anchors signed by retired keys.
    """
    if not anchor.signature or not anchor.key_id:
        return False
    k = keyring.get(anchor.key_id)
    if k is None:
        return False
    return verify_against_signed_anchor(log, anchor, k)
