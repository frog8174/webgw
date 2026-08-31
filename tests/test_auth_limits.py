"""認證、綁定安全機制、限流。

MCP 規格的傳輸安全章節:伺服器 SHOULD 對所有連線實作認證。
在此之前 gateway 對任何連得到該埠的人全部放行 —— 而這個工具的價值正是
「從你自己的網路出去爬」,無認證等於把 IP 信譽開放給任何人。
"""
from __future__ import annotations

import asyncio

import pytest

from webgw.auth import BearerAuth
from webgw.config import Config, effective_host
from webgw.limits import ConcurrencyLimiter, RateLimiter

TOKEN = "s3cret-token"


async def _call(wrapped, method="POST", path="/mcp", headers=None):
    sent = []

    async def send(msg):
        sent.append(msg)

    passed = {}

    async def inner(scope, receive, send_):
        passed["path"] = scope["path"]
        await send_({"type": "http.response.start", "status": 200, "headers": []})

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": ("1.2.3.4", 1234),
    }
    await wrapped(scope, None, send)
    return sent[0]["status"], passed


# ── 認證 ──────────────────────────────────────────────────────────
async def test_missing_token_is_rejected():
    async def inner(scope, receive, send):
        raise AssertionError("未授權的請求不該進入內層")

    status, _ = await _call(BearerAuth(inner, TOKEN))
    assert status == 401


async def test_wrong_token_is_rejected():
    async def inner(scope, receive, send):
        raise AssertionError("錯誤 token 不該進入內層")

    hdr = [(b"authorization", b"Bearer wrong-token")]
    status, _ = await _call(BearerAuth(inner, TOKEN), headers=hdr)
    assert status == 401


def _echo():
    """會回 200 並記錄收到的路徑,用來確認請求真的穿透到內層。"""
    seen: dict = {}

    async def inner(scope, receive, send):
        seen["path"] = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})

    inner.seen = seen
    return inner


async def test_correct_token_passes():
    hdr = [(b"authorization", f"Bearer {TOKEN}".encode())]
    inner = _echo()
    status, _ = await _call(BearerAuth(inner, TOKEN), headers=hdr)
    assert status == 200
    assert inner.seen["path"] == "/mcp"


async def test_healthz_stays_public():
    """k8s 的存活/就緒探針必須能在沒有憑證的情況下打通。"""
    status, _ = await _call(BearerAuth(_echo(), TOKEN), method="GET", path="/healthz")
    assert status == 200


async def test_empty_token_disables_auth():
    status, _ = await _call(BearerAuth(_echo(), ""))
    assert status == 200


async def test_scheme_must_be_bearer():
    hdr = [(b"authorization", f"Basic {TOKEN}".encode())]
    status, _ = await _call(BearerAuth(_echo(), TOKEN), headers=hdr)
    assert status == 401


# ── 綁定安全機制 ──────────────────────────────────────────────────
def test_no_token_refuses_public_bind(monkeypatch):
    """沒有 token 卻要綁 0.0.0.0 = 對外開放且不設防,強制降級為 loopback。

    做法照抄上游 crawl4ai 0.9.2 —— 它的同款設計實測擋住過一次真實暴露。
    """
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
    monkeypatch.delenv("WEBGW_AUTH_TOKEN", raising=False)
    import importlib

    from webgw import config

    importlib.reload(config)
    host, warning = config.effective_host(config.Config())
    assert host == "127.0.0.1"
    assert warning and "拒絕綁定" in warning


def test_token_allows_public_bind(monkeypatch):
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
    monkeypatch.setenv("WEBGW_AUTH_TOKEN", TOKEN)
    import importlib

    from webgw import config

    importlib.reload(config)
    host, warning = config.effective_host(config.Config())
    assert host == "0.0.0.0" and warning is None
    monkeypatch.delenv("WEBGW_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    importlib.reload(config)


def test_loopback_without_token_warns_but_allows():
    cfg = Config()
    object.__setattr__(cfg, "host", "127.0.0.1")
    object.__setattr__(cfg, "auth_token", "")
    host, warning = effective_host(cfg)
    assert host == "127.0.0.1" and warning is not None


# ── 限流 ──────────────────────────────────────────────────────────
def test_rate_limiter_blocks_over_limit():
    r = RateLimiter(max_requests=3, window_s=60)
    assert [r.allow("k") for _ in range(4)] == [True, True, True, False]


def test_rate_limiter_is_per_key():
    r = RateLimiter(max_requests=1, window_s=60)
    assert r.allow("a") is True
    assert r.allow("b") is True      # 不同來源互不影響
    assert r.allow("a") is False


def test_rate_limiter_window_expires():
    r = RateLimiter(max_requests=1, window_s=0.05)
    assert r.allow("k") is True
    assert r.allow("k") is False
    import time as _t

    _t.sleep(0.08)
    assert r.allow("k") is True


def test_rate_limiter_zero_means_unlimited():
    r = RateLimiter(max_requests=0)
    assert all(r.allow("k") for _ in range(50))


def test_rate_limiter_prune_drops_idle_keys():
    r = RateLimiter(max_requests=5, window_s=0.01)
    r.allow("gone")
    import time as _t

    _t.sleep(0.03)
    r.prune()
    assert "gone" not in r._hits


async def test_concurrency_limiter_caps_parallelism():
    """超過上限的請求要排隊,不是被拒絕 —— 抓取本來就慢,多等優於直接失敗。"""
    limiter = ConcurrencyLimiter(2)
    active = 0
    peak = 0

    async def work():
        nonlocal active, peak
        async with limiter:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert peak <= 2


async def test_concurrency_limiter_zero_means_unlimited():
    limiter = ConcurrencyLimiter(0)
    async with limiter:
        pass
