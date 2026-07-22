"""SSRF-guarded outbound HTTP fetch (stdlib only).

Products that fetch a *caller-supplied* URL (URL liveness/tamper, content-existence
attestation) must never let a customer make the operator reach internal services (localhost,
cloud metadata at 169.254.169.254, RFC1918, etc.). This module centralizes that guard.

Defenses:
  - scheme allow-list (http/https only)
  - the ACTUAL connected peer IP is validated as globally-routable AFTER connect()
    (this defeats DNS rebinding — we check the IP we really reached, not a pre-resolved one)
  - redirects are NOT followed (a 3xx is returned as-is; following it would re-open the SSRF
    hole to an internal Location, and a liveness monitor should see the redirect anyway)
  - hard response-size cap and timeout
Residual (documented): a public host that itself proxies to an internal network is out of
scope; we validate the peer, not what the peer does.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

_UA = "mandatehub-operator/1 (+https://github.com/Hiroshi-Ichiyanagi/mandatehub)"
MAX_BYTES = 2 * 1024 * 1024        # 2 MiB body cap
TIMEOUT = 12                        # seconds


class FetchError(Exception):
    """Raised for unsafe/invalid input (caller is NOT charged) — distinct from a target that
    simply responded with an error status (that is a valid, billable result)."""


def _require_public(ip: str) -> None:
    a = ipaddress.ip_address(ip)
    if (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified):
        raise FetchError(f"refusing to reach non-public address ({a})")


def safe_fetch(url: str, *, method: str = "GET", max_bytes: int = MAX_BYTES,
               timeout: int = TIMEOUT) -> dict:
    """Fetch `url` with SSRF guards. Returns a dict:
        {url, final_scheme, host, status, reason, headers{content-type,location,...},
         body_sha256, body_bytes, truncated}
    Raises FetchError on unsafe/invalid input (do not charge). A reachable target that returns
    4xx/5xx is a normal, billable result (status is reported)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise FetchError("only http/https URLs are supported")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    # Pre-resolve and reject if any resolved address is non-public (fast reject before connect).
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"DNS resolution failed: {type(e).__name__}") from None
    for info in infos:
        _require_public(info[4][0])

    if scheme == "https":
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    try:
        conn.connect()
        # Validate the IP we ACTUALLY connected to (defeats DNS-rebinding TOCTOU).
        peer = conn.sock.getpeername()[0]
        _require_public(peer)
        conn.request(method, path, headers={"User-Agent": _UA, "Accept": "*/*",
                                            "Connection": "close"})
        resp = conn.getresponse()   # redirects intentionally NOT followed
        raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        body = raw[:max_bytes]
        hdrs = {k.lower(): v for k, v in resp.getheaders()
                if k.lower() in ("content-type", "location", "content-length",
                                 "last-modified", "etag", "server")}
        return {
            "url": url,
            "scheme": scheme,
            "host": host,
            "peer_ip": peer,
            "status": resp.status,
            "reason": resp.reason,
            "headers": hdrs,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
            "truncated": truncated,
        }
    except FetchError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        # A network-level failure to reach/parse the target. This is a *result* for a liveness
        # monitor ("unreachable"), not an input error — surface it, let the product decide.
        return {"url": url, "scheme": scheme, "host": host, "status": None,
                "reason": f"unreachable: {type(e).__name__}", "headers": {},
                "body_sha256": None, "body_bytes": 0, "truncated": False}
    finally:
        conn.close()
