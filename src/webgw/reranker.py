"""Cross-encoder 重排客戶端。

為什麼用 cross-encoder 而不是 embedding:query 和段落**一起**送進模型,
模型看得到兩者的互動,比分別編碼再算相似度準得多。而且不需要存或比對向量。

為什麼是兩階段:不對整頁重排。BM25 先取前 N 名(毫秒級、不用 GPU),
只把這 N 段送給模型。一頁常有 20~200 節,差 6 倍以上的工作量。

實測 (30 個案例,bge-reranker-v2-m3 on vLLM):
    BM25 + t2s          rank@1 24/30
    再加上重排           rank@1 28/30    中位 +3 秒

多修好的 5 個全是「字面不匹配但語意相關」—— BM25 的方法邊界。
代價是延遲從 2.4 秒變成 5~8 秒,所以預設不開,由 mode 決定。
"""
from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("webgw.reranker")


class RerankUnavailable(Exception):
    """重排服務不可用。呼叫端據此降級回 BM25,而不是讓整個請求失敗。"""


class RerankClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 10.0,
        doc_chars: int = 2000,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_s
        # 單段送出的字元上限。cross-encoder 是把 query 和 document 接在一起
        # 送進去,超過 max_model_len 的部分會被截掉 —— 可能截掉答案所在處。
        self._doc_chars = doc_chars

    @property
    def configured(self) -> bool:
        return bool(self._base and self._model)

    async def order(self, query: str, documents: list[str]) -> list[int]:
        """回傳依相關性排序後的索引。失敗時丟 RerankUnavailable。"""
        if not self.configured:
            raise RerankUnavailable("reranker 未設定")
        if not documents:
            return []

        payload = {
            "model": self._model,
            "query": query,
            "documents": [d[: self._doc_chars] for d in documents],
        }
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/v1/rerank",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise RerankUnavailable(f"逾時 ({self._timeout}s)") from exc
        except httpx.HTTPError as exc:
            raise RerankUnavailable(f"{type(exc).__name__}: {str(exc)[:120]}") from exc

        if resp.status_code >= 400:
            raise RerankUnavailable(f"HTTP {resp.status_code}: {resp.text[:120]}")

        try:
            results = resp.json()["results"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RerankUnavailable(f"回應格式不符: {resp.text[:120]}") from exc

        order = [r["index"] for r in results if isinstance(r.get("index"), int)]
        # 服務可能只回前 N 名。把沒回到的補在後面,確保不遺失任何段落。
        seen = set(order)
        order.extend(i for i in range(len(documents)) if i not in seen)
        log.info("rerank %d 段,耗時 %dms", len(documents), int((time.time() - t0) * 1000))
        return order

    async def healthy(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base}/v1/models")
            return r.status_code == 200
        except httpx.HTTPError:
            return False
