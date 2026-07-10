"""SSRF-safe URL fetch helpers (extracted from server/handlers/documents.py).

Shared by the document library HTTP handlers and the ingestion pipeline
to ensure all outbound URL fetches validate resolved IPs against private/
loopback networks before connecting, and pin the connection to the
validated IP to prevent DNS-rebinding attacks.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    import ipaddress as _ip

from ohm.exceptions import ValidationError
from ohm.framework.validation import canonicalize_ip


_FETCH_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_LOOPBACK_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def _canonicalize_and_check_ip(addr: str, allow_loopback: bool) -> ipaddress._BaseAddress:
    """Canonicalize *addr* and raise ValidationError if it is private/loopback."""
    ip = canonicalize_ip(ipaddress.ip_address(addr))
    for net in _FETCH_BLOCKED_NETWORKS:
        if ip in net:
            raise ValidationError(f"URL fetch blocked: host resolves to private address {addr} (SSRF protection)")
    if not allow_loopback:
        for net in _LOOPBACK_NETWORKS:
            if ip in net:
                raise ValidationError(f"URL fetch blocked: host resolves to loopback address {addr} (SSRF protection)")
    return ip


def validate_fetch_url(url: str) -> str:
    """Validate that *url* has an allowed scheme and is safe to fetch.

    Returns the validated URL string. Raises ValidationError if the scheme
    is not http/https.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"URL fetch blocked: scheme '{parsed.scheme}' is not allowed (only http/https)")
    if not parsed.hostname:
        raise ValidationError("URL fetch blocked: no hostname in URL")
    return url


def safe_fetch_pinned(url: str, *, timeout: float = 30.0, allow_loopback: bool = True) -> tuple[bytes, str | None]:
    """Fetch *url* with DNS-rebinding mitigation by pinning the resolved IP.

    Validates all resolved addresses against private/loopback networks,
    then connects to the first validated IP while preserving the original
    Host header / TLS SNI. Does not follow redirects.

    Returns ``(content_bytes, content_type)``.
    """
    import http.client
    import ssl

    validate_fetch_url(url)

    parsed = urlparse(url)
    scheme = parsed.scheme
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise ValidationError(f"Cannot resolve fetch URL host: {host!r}")

    validated: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr in validated:
            continue
        _canonicalize_and_check_ip(addr, allow_loopback)
        validated.append(addr)

    if not validated:
        raise ValidationError(f"Cannot resolve fetch URL host: {host!r}")

    pinned_ip = validated[0]
    headers = {"Host": host, "User-Agent": "OHM/1.0"}

    if scheme == "http":
        conn = http.client.HTTPConnection(pinned_ip, port, timeout=timeout)
    else:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(pinned_ip, port, timeout=timeout, context=ctx)
        conn.server_hostname = host

    try:
        conn.request("GET", request_path, headers=headers)
        resp = conn.getresponse()
        if resp.status >= 400:
            raise ValidationError(f"URL fetch failed: HTTP {resp.status} {resp.reason}")
        content_bytes = resp.read()
        detected_type = resp.headers.get("Content-Type")
        return content_bytes, detected_type
    finally:
        conn.close()


def validate_local_path(path: str, root: str | None = None) -> str:
    """Validate that *path* is safe to read from.

    If *root* is provided, the path must resolve to a location within
    the root directory. Symlinks and traversal escapes are rejected.

    Returns the resolved path string. Raises ValidationError if the path
    escapes the root or contains traversal sequences.
    """
    from pathlib import Path

    if not path:
        raise ValidationError("local_path must be non-empty")

    p = Path(path)

    if root:
        root_path = Path(root).resolve()
        resolved = p.resolve()
        try:
            resolved.relative_to(root_path)
        except ValueError:
            raise ValidationError(f"local_path '{path}' escapes ingestion root '{root}'")
        return str(resolved)

    if ".." in p.parts:
        raise ValidationError(f"local_path '{path}' contains path traversal sequence")
    if p.is_symlink():
        raise ValidationError(f"local_path '{path}' is a symlink (not allowed)")

    return str(p)