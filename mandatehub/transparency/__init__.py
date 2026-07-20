"""
transparency — Merkle ツリー + ハッシュチェーン監査ログ（vendored 検証プリミティブ）。

mandatehub が必要とする最小限のみ。proof-of-reserves 等は含まない
（intent / execution が独自の証明を持つ）。標準ライブラリのみ。
"""

from mandatehub.transparency.audit_log import GENESIS_HASH, AuditEvent, AuditLog
from mandatehub.transparency.audit_query import audit_root_as_of
from mandatehub.transparency.merkle import (
    MerkleLeaf,
    MerkleProof,
    MerkleProofStep,
    MerkleTree,
    hash_pair,
    sha256_hex,
    verify_proof_with_node_prefix,
)

__all__ = [
    "GENESIS_HASH",
    "AuditEvent",
    "AuditLog",
    "audit_root_as_of",
    "MerkleLeaf",
    "MerkleProof",
    "MerkleProofStep",
    "MerkleTree",
    "hash_pair",
    "sha256_hex",
    "verify_proof_with_node_prefix",
]
