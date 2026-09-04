"""Cross-encoder reranking client.

Why a cross-encoder rather than embeddings: the query and the passage go into
the model *together*, so it sees their interaction. That is markedly more
accurate than encoding each separately and comparing vectors, and it needs no
vector store.

Why two stages: the whole page is never reranked. BM25 picks the top N first
(milliseconds, no GPU), and only those go to the model. Pages commonly hold
20-200 sections, so this is a 6x or larger difference in model work.

Measured on 30 cases with bge-reranker-v2-m3 on vLLM:
    BM25 + script normalization    rank@1 24/30
    plus reranking                 rank@1 28/30    median +3 seconds

All 5 additional fixes were "no literal overlap but semantically related",
which is the boundary of the BM25 method. The cost is latency going from about
2.4 seconds to 5-8, so this is off by default and selected per call via `mode`.

Wire format: the request and response match the Cohere rerank API, which Jina
and others also implement, so a self-hosted vLLM endpoint and a commercial
service are interchangeable. The only difference is authentication -- set
api_key for commercial providers, leave it empty for self-hosted.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("webgw.reranker")


class RerankUnavailable(Exception):
    """Reranking service is unusable.

    Callers degrade to BM25 on this rather than failing the whole request.
    """


class RerankClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 10.0,
        doc_chars: int = 2000,
        api_key: str = "",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_s
        # Character cap per passage. A cross-encoder concatenates query and
        # document before encoding, and anything past max_model_len is cut --
        # possibly cutting off where the answer sits.
        self._doc_chars = doc_chars
        # Self-hosted endpoints usually have no auth; commercial ones always do.
        # Left empty, the header is not sent at all.
        self._api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self._base and self._model)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def order(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """Return [(index, relevance_score), ...], most relevant first.

        The scores must come back with the indices. Without them the caller can
        only reuse BM25's statistics, so the reported match.confidence would
        describe how certain *BM25* was rather than the reranker -- measured
        making both modes report identical numbers while selecting different
        sections.
        """
        if not self.configured:
            raise RerankUnavailable("reranker not configured")
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
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise RerankUnavailable(f"timeout ({self._timeout}s)") from exc
        except httpx.HTTPError as exc:
            raise RerankUnavailable(f"{type(exc).__name__}: {str(exc)[:120]}") from exc

        if resp.status_code >= 400:
            raise RerankUnavailable(f"HTTP {resp.status_code}: {resp.text[:120]}")

        try:
            results = resp.json()["results"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RerankUnavailable(f"unexpected response shape: {resp.text[:120]}") from exc

        scored = [
            (r["index"], float(r.get("relevance_score", 0.0)))
            for r in results
            if isinstance(r.get("index"), int)
        ]
        # The service may return only the top N. Anything missing is appended
        # with a score of 0 so no section is lost.
        seen = {i for i, _ in scored}
        scored.extend((i, 0.0) for i in range(len(documents)) if i not in seen)
        log.info("reranked %d sections in %dms", len(documents), int((time.time() - t0) * 1000))
        return scored

    async def healthy(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base}/v1/models", headers=self._headers())
            return r.status_code == 200
        except httpx.HTTPError:
            return False
