"""Vendored Ed25519 verify — pure Python, ZERO third-party dependencies.

Source: the public-domain Ed25519 reference implementation by D. J. Bernstein
et al. (https://ed25519.cr.yp.to/python/ed25519.py), matching RFC 8032. Only
the verification path is used here. It is intentionally the slow, simple
reference (one signature verify per ledger is fine); it is NOT constant-time,
but verification handles only public data so that is acceptable.

This file exists because the Python standard library has no Ed25519. Vendoring
it keeps the verifier dependency-free (stdlib + this file only).
"""

import hashlib

b = 256
q = 2 ** 255 - 19
l = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m):
    return hashlib.sha512(m).digest()


def _expmod(base, e, m):
    if e == 0:
        return 1
    t = _expmod(base, e // 2, m) ** 2 % m
    if e & 1:
        t = (t * base) % m
    return t


def _inv(x):
    return _expmod(x, q - 2, q)


d = -121665 * _inv(121666) % q
I = _expmod(2, (q - 1) // 4, q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(d * y * y + 1)
    x = _expmod(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * I) % q
    if x % 2 != 0:
        x = q - x
    return x


By = 4 * _inv(5) % q
Bx = _xrecover(By)
B = [Bx % q, By % q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - d * x1 * x2 * y1 * y2)
    return [x3 % q, y3 % q]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(b // 8))


def _decodeint(s):
    # Little-endian decode of the FULL byte string (the scalar S is 32 bytes;
    # the hash-derived h is 64 bytes = 512 bits — must not be truncated).
    return int.from_bytes(s, "little")


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - d * x * x * y * y) % q == 0


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, b - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, b - 1):
        x = q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True iff `signature` is a valid Ed25519 signature of `message`
    under `public_key` (RFC 8032). Never raises — bad inputs return False."""
    try:
        if len(signature) != b // 4 or len(public_key) != b // 8:
            return False
        R = _decodepoint(signature[: b // 8])
        A = _decodepoint(public_key)
        S = _decodeint(signature[b // 8 : b // 4])
        h = _decodeint(_H(_encodepoint(R) + public_key + message))
        return _scalarmult(B, S) == _edwards(R, _scalarmult(A, h))
    except Exception:
        return False
