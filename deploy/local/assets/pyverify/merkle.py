"""RFC 6962 Merkle transparency-log — independent Python implementation.

Mirrors `govern-merkle` (Rust): domain-separated leaf/node hashing, the Merkle
Tree Hash (root), inclusion-proof reconstruction (CT decomposition), and
consistency verification (RFC 6962-bis §2.1.4.2). Pure stdlib (`hashlib`).

This is the independent second implementation of the RFC 6962 capability added
to govern in the sigil integration: a third party can recompute the tree root
and re-verify inclusion/consistency proofs offline, in a language other than the
one that produced them. It is additive — it does not replace govern's existing
tessera trace-commitment Merkle or the record-hash chains.
"""

import hashlib


def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def empty_root() -> bytes:
    return _sha256()


def leaf_hash(leaf: bytes) -> bytes:
    return _sha256(b"\x00", leaf)


def node_hash(left: bytes, right: bytes) -> bytes:
    return _sha256(b"\x01", left, right)


def _split(n: int) -> int:
    """Largest power of two strictly less than n (for n >= 2)."""
    k = 1
    while (k << 1) < n:
        k <<= 1
    return k


def _mth(hashes: list[bytes]) -> bytes:
    n = len(hashes)
    if n == 0:
        return empty_root()
    if n == 1:
        return hashes[0]
    k = _split(n)
    return node_hash(_mth(hashes[:k]), _mth(hashes[k:]))


def leaf_hashes(leaves: list[bytes]) -> list[bytes]:
    return [leaf_hash(l) for l in leaves]


def root(leaves: list[bytes]) -> bytes:
    """Merkle Tree Hash (root) over raw leaf data."""
    return _mth(leaf_hashes(leaves))


def _inner_proof_size(index: int, size: int) -> int:
    return (index ^ (size - 1)).bit_length()


def root_from_inclusion(leaf_h: bytes, index: int, tree_size: int, proof: list[bytes]):
    if index >= tree_size:
        return None
    inner = _inner_proof_size(index, tree_size)
    border = bin(index >> inner).count("1")
    if len(proof) != inner + border:
        return None
    res = leaf_h
    for i in range(inner):
        if (index >> i) & 1 == 0:
            res = node_hash(res, proof[i])
        else:
            res = node_hash(proof[i], res)
    for i in range(inner, len(proof)):
        res = node_hash(proof[i], res)
    return res


def verify_inclusion(leaf_h, index, tree_size, proof, root) -> bool:
    r = root_from_inclusion(leaf_h, index, tree_size, proof)
    return r is not None and r == root


def verify_consistency(first: int, second: int, proof: list[bytes], first_root: bytes,
                       second_root: bytes) -> bool:
    if first > second:
        return False
    if first == second:
        return len(proof) == 0 and first_root == second_root
    if first == 0:
        return len(proof) == 0

    # If `first` is an exact power of 2, prepend the prior root (not carried in proof).
    path = []
    if first & (first - 1) == 0:
        path.append(first_root)
    path.extend(proof)
    if not path:
        return False

    fn = first - 1
    sn = second - 1
    while fn & 1 == 1:
        fn >>= 1
        sn >>= 1

    fr = path[0]
    sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 == 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            while fn != 0 and fn & 1 == 0:
                fn >>= 1
                sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1

    return sn == 0 and fr == first_root and sr == second_root
