"""Admission layer. The rules come from four consecutive SSRF CVEs upstream
across 0.8.7-0.9.0."""
from __future__ import annotations

import pytest

from webgw import admission


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1:6379/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:11235/health",
    ],
)
def test_forbidden_destinations(url):
    assert admission.check(url, resolve=False).allowed is False


@pytest.mark.parametrize("url", ["https://arxiv.org/pdf/1706.03762.pdf", "https://x.com/a.zip"])
def test_binary_blocked_preflight(url):
    """PDFs were measured crashing upstream outright (Page.goto: Download is
    starting), so they are rejected before dispatch."""
    v = admission.check(url, resolve=False)
    assert v.allowed is False and v.reason == "unsupported_content"


def test_public_url_allowed():
    assert admission.check("https://example.com/page", resolve=False).allowed is True


def test_redirect_to_private_is_blocked():
    v = admission.check_redirect("https://example.com/", "http://169.254.169.254/")
    assert v.allowed is False and v.reason == "blocked_redirect"


def test_redirect_to_public_is_allowed():
    v = admission.check_redirect(
        "https://github.com/ggerganov/llama.cpp", "https://github.com/ggml-org/llama.cpp"
    )
    assert v.allowed is True


def test_allowed_hosts_expand_to_wildcard_ports():
    """Hosts given without a port must also match any port.

    The upstream Host check is an exact match including the port, and a NodePort
    deployment presents `<node IP>:<nodePort>` -- without expansion every
    request is rejected with 421.
    """
    from webgw.config import expand_allowed_hosts

    assert expand_allowed_hosts(("127.0.0.1", "localhost")) == [
        "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
    ]
    # Hosts that already carry a port are left alone, not expanded again.
    assert expand_allowed_hosts(("192.168.1.60:30080",)) == ["192.168.1.60:30080"]
