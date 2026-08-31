"""URL 准入。送出前擋掉不該爬的目的地。

與上游 crawl4ai 的關係:0.9.2 內建 egress pinning proxy,實測會解析 DNS 後檢查真實 IP
(`169.254.169.254.nip.io` 被擋),且 /crawl 與 /crawl/stream 兩條路徑都套用。
所以這一層是**第二道防線**而非唯一防線 —— 但仍然必做,因為該專案有四次
「檢查存在但有路徑沒套到」的紀錄 (0.8.7~0.9.0 連續四個 CVE)。

重要區分:本模組只檢查**使用者要求爬取的 URL**。gateway 連往上游 crawl4ai 的位址
(可能是區網 IP) 不經過這裡。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}

# crawl4ai 是 HTML pipeline。實測 arXiv PDF 會讓它硬崩潰:
#   RuntimeError: Failed on navigating ACS-GOTO: Page.goto: Download is starting
# 所以在送出前就擋掉,不要讓它崩。
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
    # IPv4-mapped / 6to4 / NAT64 等轉換形式 —— 上游 0.8.8 就是被這類繞過打穿的。
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

    # 字面 IP 直接檢查,不必經過 DNS。
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

    # 解析後檢查**所有**回傳位址 —— 只檢查第一個會被多 A 記錄繞過。
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
    """轉址後重新驗證最終落點。

    上游是否在 redirect 途中重驗,實測沒能確認 (測試在 pre-flight 階段就被擋下,
    沒進到 redirect 階段)。在確認之前這一層必做:公開網址 302 到私網就是 SSRF。
    """
    if not final or final == original:
        return Verdict(True)
    verdict = check(final)
    if not verdict.allowed:
        return Verdict(False, "blocked_redirect", f"{original} -> {final}: {verdict.detail}")
    return Verdict(True)
