"""併發與速率限制。

為什麼需要:上游 crawl4ai 啟動時會記錄 `work queue (per_principal=unlimited)`,
它自己沒有任何限流。而每次 web_fetch 都會開一個真實的瀏覽器分頁 —— 那是
記憶體與 CPU 的重負載。沒有上限的話,幾個並發請求就能把上游拖垮。

兩層分開處理,因為它們防的是不同的事:
  併發上限  同一時間最多幾個抓取在跑 —— 保護上游的資源
  速率上限  每個來源每分鐘最多幾次請求 —— 防止單一客戶端佔滿配額
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class ConcurrencyLimiter:
    """限制同時進行的抓取數量。

    用 asyncio.Semaphore:超過上限的請求會排隊等待,而不是被拒絕 ——
    抓取本來就慢(實測 0.3~21 秒),多等一下比直接失敗對 agent 有用得多。
    """

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit) if limit > 0 else None
        self.limit = limit

    async def __aenter__(self):
        if self._sem is not None:
            await self._sem.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._sem is not None:
            self._sem.release()


class RateLimiter:
    """每個來源在滑動視窗內的請求數上限。

    滑動視窗(sliding window):記錄每次請求的時間戳,只保留視窗內的,
    數量超過上限就拒絕。比固定視窗準確 —— 固定視窗在邊界處可以擠進兩倍流量。
    """

    def __init__(self, max_requests: int, window_s: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_s
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def retry_after_s(self, key: str) -> int:
        """還要等幾秒才會有額度。用來給呼叫方明確的等待時間。"""
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, int(self.window_s - (time.monotonic() - hits[0])) + 1)

    def prune(self) -> None:
        """清掉沒有近期活動的來源,避免長時間執行後字典無限成長。"""
        cutoff = time.monotonic() - self.window_s
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)
