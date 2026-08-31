"""准入層測試。規則來自上游 0.8.7~0.9.0 連續四個 SSRF CVE 的教訓。"""
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
    """實測 PDF 會讓上游硬崩潰 (Page.goto: Download is starting),在送出前擋掉。"""
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
    """不含埠的主機要自動允許任意埠。

    上游 Host 比對是完全相符含埠號的,NodePort 的 Host 會是 <節點IP>:<nodePort>,
    不展開就會全部被擋成 421。
    """
    from webgw.config import expand_allowed_hosts

    assert expand_allowed_hosts(("127.0.0.1", "localhost")) == [
        "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
    ]
    # 已經帶埠的維持原樣,不重複展開
    assert expand_allowed_hosts(("192.168.1.60:30080",)) == ["192.168.1.60:30080"]
