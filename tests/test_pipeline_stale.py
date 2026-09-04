"""The stale fallback path.

When a fetch fails but an in-retention copy is still cached, the old copy is
served and flagged rather than returning an error code. The reason is measured:
anti-bot blocking is common (Reuters via DataDome, Medium via Cloudflare), and
three-day-old content beats an error the agent cannot use.
"""
from __future__ import annotations

import time

import pytest

from webgw import outcome as oc
from webgw import pipeline
from webgw.cache import CacheStore
from webgw.config import Config
from webgw.crawl_client import CrawlResult

# Deliberately non-ASCII: this fixture also exercises the UTF-8 paths through
# sectioning, token counting and SQLite storage.
PAGE = "# 標題\n\n" + ("內容段落。" * 200)


class StubClient:
    """Fake upstream that can be switched between success and failure."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls = 0

    async def fetch(self, url: str) -> CrawlResult:
        self.calls += 1
        if self.mode == "transport_fail":
            return CrawlResult(False, {}, "timeout")
        if self.mode == "antibot":
            return CrawlResult(True, {
                "success": False, "status_code": 401,
                "error_message": "Blocked by anti-bot protection: DataDome captcha",
                "markdown": {"raw_markdown": "\n"},
            })
        return CrawlResult(True, {
            "success": True, "status_code": 200,
            "markdown": {"raw_markdown": PAGE},
            "metadata": {"title": "T"},
        })

    @staticmethod
    def raw_markdown(result: dict) -> str:
        md = result.get("markdown")
        return md.get("raw_markdown", "") if isinstance(md, dict) else (md or "")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Bypass the admission layer's DNS resolution -- unit tests must not depend
    on the network.

    The admission logic itself is verified independently in test_admission.py
    with resolve=False.
    """
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    monkeypatch.setattr(
        pipeline.admission, "check", lambda url, **kw: admission_ok()
    )


def admission_ok():
    from webgw.admission import Verdict

    return Verdict(True)


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path) -> CacheStore:
    return CacheStore(str(tmp_path / "c.sqlite3"), retention_days=14, default_max_age_s=60)


def _age_entry(store: CacheStore, seconds: int) -> None:
    conn = store._connect()
    conn.execute("UPDATE pages SET fetched_at=?", (int(time.time()) - seconds,))
    conn.commit()
    conn.close()


async def test_fresh_cache_avoids_upstream_call(cfg, store):
    client = StubClient("ok")

    first = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert first["cache"] == "miss" and client.calls == 1

    second = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert second["cache"] == "hit"
    assert client.calls == 1          # upstream was not called again


async def test_stale_served_when_upstream_transport_fails(cfg, store):
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 3600)           # past the 60s freshness, far inside 14-day retention
    client.mode = "transport_fail"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.OK
    assert res["cache"] == "stale"
    assert res["cache_age_s"] >= 3600
    assert "Re-fetch failed" in res["note"]
    assert res["content"] or res["excerpts"]


async def test_stale_served_when_site_blocks(cfg, store, monkeypatch):
    """Old content is served on anti-bot blocks too -- the main real-world use
    of this path."""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 3600)
    client.mode = "antibot"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.OK and res["cache"] == "stale"
    assert oc.BLOCKED_ANTIBOT in res["note"]


async def test_no_cache_entry_returns_real_error(cfg, store, monkeypatch):
    """With no usable old copy, the failure must be reported honestly rather
    than dressed up as success."""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("antibot")

    res = await pipeline.fetch("https://e.com/never-cached", None, cfg, client, store)
    assert res["outcome"] == oc.BLOCKED_ANTIBOT
    assert res["retryable"] is False
    assert res["content"] is None


async def test_expired_beyond_retention_is_not_served_as_stale(cfg, store, monkeypatch):
    """Past retention the data is gone and must not come back as stale."""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 15 * 86_400)    # past 14 days
    client.mode = "antibot"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.BLOCKED_ANTIBOT
