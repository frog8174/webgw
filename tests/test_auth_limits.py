"""Authentication, the binding safeguard, and rate limiting.

The MCP transport security section says servers SHOULD authenticate all
connections. Before this existed the gateway served anyone who could reach the
port -- and the value of this tool is precisely that it crawls from your own
network, so running unauthenticated hands your IP reputation to anyone.
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


# -- authentication ----------------------------------------------------------
async def test_missing_token_is_rejected():
    async def inner(scope, receive, send):
        raise AssertionError("an unauthorized request must not reach the inner app")

    status, _ = await _call(BearerAuth(inner, TOKEN))
    assert status == 401


async def test_wrong_token_is_rejected():
    async def inner(scope, receive, send):
        raise AssertionError("a wrong token must not reach the inner app")

    hdr = [(b"authorization", b"Bearer wrong-token")]
    status, _ = await _call(BearerAuth(inner, TOKEN), headers=hdr)
    assert status == 401


def _echo():
    """Returns 200 and records the path, confirming the request really got through."""
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
    """Kubernetes liveness and readiness probes must work without credentials."""
    status, _ = await _call(BearerAuth(_echo(), TOKEN), method="GET", path="/healthz")
    assert status == 200


async def test_empty_token_disables_auth():
    status, _ = await _call(BearerAuth(_echo(), ""))
    assert status == 200


async def test_scheme_must_be_bearer():
    hdr = [(b"authorization", f"Basic {TOKEN}".encode())]
    status, _ = await _call(BearerAuth(_echo(), TOKEN), headers=hdr)
    assert status == 401


# -- binding safeguard -------------------------------------------------------
def test_no_token_refuses_public_bind(monkeypatch):
    """No token plus 0.0.0.0 means publicly reachable and unprotected, so the
    binding is forced down to loopback.

    The approach is taken from upstream crawl4ai 0.9.2, whose equivalent design
    was measured catching a real exposure.
    """
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
    monkeypatch.delenv("WEBGW_AUTH_TOKEN", raising=False)
    import importlib

    from webgw import config

    importlib.reload(config)
    host, warning = config.effective_host(config.Config())
    assert host == "127.0.0.1"
    assert warning and "refusing to bind" in warning


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


# -- rate limiting -----------------------------------------------------------
def test_rate_limiter_blocks_over_limit():
    r = RateLimiter(max_requests=3, window_s=60)
    assert [r.allow("k") for _ in range(4)] == [True, True, True, False]


def test_rate_limiter_is_per_key():
    r = RateLimiter(max_requests=1, window_s=60)
    assert r.allow("a") is True
    assert r.allow("b") is True      # separate callers do not affect each other
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
    """Requests over the limit queue rather than being rejected -- fetching is
    slow anyway, so waiting beats failing outright."""
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
