"""URL admission. Rejects destinations that must not be crawled, before sending.

Relationship to upstream crawl4ai: 0.9.2 ships an egress pinning proxy that was
measured resolving DNS and checking the real IP (`169.254.169.254.nip.io` was
blocked), applied on both /crawl and /crawl/stream. So this layer is a *second*
line of defence rather than the only one -- but it is still required, because
that project has a track record of "the check exists but one path does not
apply it": four consecutive CVEs across 0.8.7-0.9.0.

Important distinction: this module only checks the URL *the user asked to
crawl*. The address the gateway uses to reach upstream crawl4ai (possibly a
private LAN IP) does not pass through here.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}

# crawl4ai is an HTML pipeline. An arXiv PDF was measured crashing it outright:
#   RuntimeError: Failed on navigating ACS-GOTO: Page.goto: Download is starting
# So these are rejected before dispatch rather than left to crash upstream.
BINARY_SUFFIXES = (
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".exe", ".dmg", ".msi", ".deb", ".rpm", ".apk",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    detail: str = ""


def _ip_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # IPv4-mapped, 6to4, NAT64 and similar transition forms -- this class of
    # bypass is exactly what broke upstream 0.8.8.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or getattr(ip, "sixtofour", None)
        if mapped is not None and _ip_is_forbidden(mapped):
            return True
    return False


def check(url: str, *, resolve: bool = True) -> Verdict:
    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        return Verdict(False, "invalid_url", str(exc)[:120])

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return Verdict(False, "blocked_url", f"scheme not allowed: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        return Verdict(False, "invalid_url", "no host")

    path = (parsed.path or "").lower()
    for suf in BINARY_SUFFIXES:
        if path.endswith(suf):
            return Verdict(False, "unsupported_content", f"binary/document type: {suf}")

    # Literal IPs are checked directly, no DNS needed.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_forbidden(literal):
            return Verdict(False, "blocked_url", f"forbidden address: {host}")
        return Verdict(True)

    if not resolve:
        return Verdict(True)

    # Check *every* resolved address -- inspecting only the first one is
    # bypassable with multiple A records.
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return Verdict(False, "dns_error", str(exc)[:120])

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if _ip_is_forbidden(ip):
            return Verdict(False, "blocked_url", f"{host} resolves to forbidden address {ip}")

    return Verdict(True)


def check_redirect(original: str, final: str | None) -> Verdict:
    """Re-validate the final destination after redirects.

    Whether upstream re-validates mid-redirect could not be confirmed by
    measurement (the test was rejected at the pre-flight stage and never
    reached the redirect stage). Until that is confirmed this layer is
    required: a public URL that 302s into a private network is SSRF.
    """
    if not final or final == original:
        return Verdict(True)
    verdict = check(final)
    if not verdict.allowed:
        return Verdict(False, "blocked_redirect", f"{original} -> {final}: {verdict.detail}")
    return Verdict(True)
