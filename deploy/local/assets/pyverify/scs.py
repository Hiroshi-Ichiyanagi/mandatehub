"""SCS — Spendcap Canonical Serialization, an INDEPENDENT Python reimplementation
that must produce BYTE-IDENTICAL output to govern-core's `to_scs_bytes`.

Canonical JSON: object keys sorted by UTF-16 code units (RFC 8785 §3.2.3, JCS),
no whitespace, integers only (floats/exponents/out-of-range rejected), minimal
string escaping (`"` `\\` and control chars; `\\u00xx` lowercase). This is RFC
8785 (JCS) restricted to integer documents. This byte-for-byte match with the
Rust side is the crux of cross-implementation fidelity.
"""

from typing import Any

I64_MIN = -(2 ** 63)
U64_MAX = 2 ** 64 - 1


class ScsError(Exception):
    pass


def to_scs_bytes(value: Any) -> bytes:
    out = bytearray()
    _write(value, out)
    return bytes(out)


def _write(value: Any, out: bytearray) -> None:
    # NOTE: bool is a subclass of int in Python — check it FIRST.
    if value is None:
        out += b"null"
    elif value is True:
        out += b"true"
    elif value is False:
        out += b"false"
    elif isinstance(value, int):
        # serde_json accepts only what fits i64 or u64; reject anything else.
        if not (I64_MIN <= value <= U64_MAX):
            raise ScsError(f"non-integer/out-of-range number: {value}")
        out += str(value).encode("ascii")
    elif isinstance(value, float):
        raise ScsError("SCS forbids floats")
    elif isinstance(value, str):
        _write_string(value, out)
    elif isinstance(value, list):
        out += b"["
        for i, item in enumerate(value):
            if i > 0:
                out += b","
            _write(item, out)
        out += b"]"
    elif isinstance(value, dict):
        # RFC 8785 §3.2.3 (JCS): sort by the UTF-16 code unit sequence of each
        # key. Comparing UTF-16-BE byte sequences is equivalent to comparing the
        # code unit sequences numerically, and matches Rust's encode_utf16 order.
        keys = sorted(value.keys(), key=lambda k: k.encode("utf-16-be"))
        out += b"{"
        for i, k in enumerate(keys):
            if i > 0:
                out += b","
            _write_string(k, out)
            out += b":"
            _write(value[k], out)
        out += b"}"
    else:
        raise ScsError(f"unsupported SCS type: {type(value)}")


def _write_string(s: str, out: bytearray) -> None:
    out += b'"'
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out += b'\\"'
        elif ch == "\\":
            out += b"\\\\"
        elif c == 0x08:
            out += b"\\b"
        elif c == 0x0C:
            out += b"\\f"
        elif ch == "\n":
            out += b"\\n"
        elif ch == "\r":
            out += b"\\r"
        elif ch == "\t":
            out += b"\\t"
        elif c < 0x20:
            out += ("\\u%04x" % c).encode("ascii")
        else:
            out += ch.encode("utf-8")
    out += b'"'
