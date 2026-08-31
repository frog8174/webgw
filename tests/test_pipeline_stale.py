"""stale 回退路徑。

抓取失敗但快取裡還有保留期內的舊資料時,回舊資料並標記,而不是回錯誤碼。
理由來自實測:反爬阻擋很常見 (Reuters DataDome / Medium Cloudflare),
此時一份三天前的內容遠勝於一個 agent 無法使用的錯誤。
"""
from __future__ import annotations

import time

import pytest

from webgw import outcome as oc
from webgw import pipeline
from webgw.cache import CacheStore
from webgw.config import Config
from webgw.crawl_client import CrawlResult

PAGE = "# 標題\n\n" + ("內容段落。" * 200)


class StubClient:
    """可切換成功/失敗的假上游。"""

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
    """繞過准入層的 DNS 解析 —— 單元測試不該依賴網路。

    准入邏輯本身由 test_admission.py 以 resolve=False 獨立驗證。
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
    assert client.calls == 1          # 沒有再打上游


async def test_stale_served_when_upstream_transport_fails(cfg, store):
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 3600)           # 超過 60 秒新鮮度,但遠在 14 天保留期內
    client.mode = "transport_fail"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.OK
    assert res["cache"] == "stale"
    assert res["cache_age_s"] >= 3600
    assert "重新抓取失敗" in res["note"]
    assert res["content"] or res["excerpts"]


async def test_stale_served_when_site_blocks(cfg, store, monkeypatch):
    """反爬阻擋時也回舊資料 —— 這是這條路徑最主要的實際用途。"""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 3600)
    client.mode = "antibot"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.OK and res["cache"] == "stale"
    assert oc.BLOCKED_ANTIBOT in res["note"]


async def test_no_cache_entry_returns_real_error(cfg, store, monkeypatch):
    """沒有可用舊資料時,必須誠實回報失敗,不能假裝成功。"""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("antibot")

    res = await pipeline.fetch("https://e.com/never-cached", None, cfg, client, store)
    assert res["outcome"] == oc.BLOCKED_ANTIBOT
    assert res["retryable"] is False
    assert res["content"] is None


async def test_expired_beyond_retention_is_not_served_as_stale(cfg, store, monkeypatch):
    """超過保留期就是不可用,不能再當 stale 回。"""
    monkeypatch.setattr(pipeline, "CrawlClient", StubClient)
    client = StubClient("ok")
    await pipeline.fetch("https://e.com/a", None, cfg, client, store)

    _age_entry(store, 15 * 86_400)    # 超過 14 天
    client.mode = "antibot"

    res = await pipeline.fetch("https://e.com/a", None, cfg, client, store)
    assert res["outcome"] == oc.BLOCKED_ANTIBOT
