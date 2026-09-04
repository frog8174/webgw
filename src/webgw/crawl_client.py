"""crawl4ai client.

Always uses /crawl/stream, even for a single URL.

The reason is measured. For the same failure, /crawl collapses into an opaque
HTTP 500:
    {"error":"Internal server error","correlation_id":"37aa5a7a8447"}
while /crawl/stream reports the full picture in band:
    {"success":false,"status_code":401,
     "error_message":"Blocked by anti-bot protection: DataDome captcha"}
Only the stream endpoint names the protection that fired -- on /crawl you would
have to dig through container logs and look the correlation_id up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


@dataclass
class CrawlResult:
    ok: bool                    # transport succeeded (not that the crawl did)
    result: dict                # single upstream result row
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
            # content_filter is deliberately omitted. PruningContentFilter was
            # measured dropping article headings while keeping login widgets
            # (one TechNews outline went from 9 sections to 2), so fit_markdown
            # is never used.
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
                        # End-of-stream sentinel row, not a result.
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
