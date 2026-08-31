"""crawl4ai 客戶端。

一律走 /crawl/stream,即使只有一個 URL。

理由是實測:同一個失敗,/crawl 收斂成不透明的 HTTP 500
    {"error":"Internal server error","correlation_id":"37aa5a7a8447"}
而 /crawl/stream 給出 in-band 的完整資訊
    {"success":false,"status_code":401,
     "error_message":"Blocked by anti-bot protection: DataDome captcha"}
指名的協定字串只有 stream 端點拿得到 —— /crawl 那條要去撈容器日誌用 correlation_id 反查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


@dataclass
class CrawlResult:
    ok: bool                    # 傳輸層是否成功 (不代表爬取成功)
    result: dict                # 上游單筆結果
    transport_error: str = ""


class CrawlClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._timeout = timeout_s

    async def fetch(self, url: str) -> CrawlResult:
        payload = {
            "urls": [url],
            # 刻意不帶 content_filter。實測 PruningContentFilter 會砍掉文章標題卻留下
            # 登入元件 (TechNews 的 outline 從 9 節掉到 2 節),fit_markdown 一律不用。
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"page_timeout": int(self._timeout * 1000)},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout + 10) as client:
                async with client.stream(
                    "POST", f"{self._base}/crawl/stream",
                    json=payload, headers=self._headers,
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        return CrawlResult(False, {}, f"HTTP {resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # 串流結尾的哨兵行,不是結果。
                        if obj.get("status") == "completed":
                            continue
                        return CrawlResult(True, obj)
            return CrawlResult(False, {}, "upstream returned no result rows")
        except httpx.TimeoutException:
            return CrawlResult(False, {}, "timeout")
        except httpx.HTTPError as exc:
            return CrawlResult(False, {}, f"{type(exc).__name__}: {str(exc)[:180]}")

    @staticmethod
    def raw_markdown(result: dict) -> str:
        md = result.get("markdown")
        if isinstance(md, dict):
            return md.get("raw_markdown") or ""
        if isinstance(md, str):
            return md
        return ""
