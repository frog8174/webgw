"""檢索模式編排:決定要不要重排,以及失敗時如何降級。

兩種模式:
    bm25    BM25 + 繁簡正規化。約 2.5 秒。預設。
    rerank  BM25 取前 N 名 -> cross-encoder 重排。約 5~8 秒。

實測 (30 個 ground-truth 案例):
    bm25    rank@1 24/30
    rerank  rank@1 28/30

── 為什麼沒有 auto 模式 ────────────────────────────────────────────

原本想做「BM25 沒把握時才重排」,但實驗證明做不到:

  訊號              正確組中位    錯誤組中位    是否重疊
  有分數段落佔比        0.62        0.51        是
  最高分             12.23        9.28        是
  分數差距            1.64        1.46        是
  查詢詞覆蓋率          0.88        0.94        是
  前 3 名集中度         0.52        0.43        是
  段落總數            22.50       95.50        是

六個訊號全部重疊。最極端的反例:「can I still build with Bazel」的
score_gap 是 2.09、信心 high、查詢詞覆蓋率 1.00 —— 而 BM25 把它排第 19。

BM25 的分數只說明它有多「確定」,不說明它對不對,而錯得很確定正是要抓的情況。
以頁面大小當閾值也無效:節數 >= 20 觸發 87%(等於全開),>= 30 只剩 25/30
(比純 BM25 多 1 個)。以「全文裝不裝得下預算」判斷更糟 —— 30 頁裡只有 1 頁
裝得下,會觸發 97%。

所以決定權交給呼叫端。agent 知道我們不知道的事:這題重不重要、
是不是已經試過一次沒找到、現在能不能等。而且 raw 已經有快取,
用 mode="rerank" 重試同一頁不需要重爬,只付重排那幾秒。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .ranking import Ranking, rank
from .reranker import RerankClient, RerankUnavailable
from .sections import Section

log = logging.getLogger("webgw.retrieval")

MODES = ("bm25", "rerank")
DEFAULT_MODE = "bm25"


@dataclass
class RetrievalOutcome:
    ranking: Ranking
    mode_used: str
    mode_requested: str
    degraded: str | None      # 有值代表想重排但失敗,已降級為 BM25
    elapsed_ms: int

    def as_dict(self) -> dict:
        d = {"mode": self.mode_used, "elapsed_ms": self.elapsed_ms}
        if self.mode_used != self.mode_requested:
            d["requested"] = self.mode_requested
        if self.degraded:
            d["degraded"] = self.degraded
        return d


def normalize_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in MODES else DEFAULT_MODE


async def retrieve(
    sections: list[Section],
    query: str | None,
    mode: str,
    client: RerankClient | None,
    *,
    top_n: int = 30,
) -> RetrievalOutcome:
    t0 = time.time()
    requested = normalize_mode(mode)
    base = rank(sections, query)

    def done(r: Ranking, used: str, degraded: str | None = None) -> RetrievalOutcome:
        return RetrievalOutcome(r, used, requested, degraded, int((time.time() - t0) * 1000))

    if not base.matched:
        return done(base, "document_order")
    if requested != "rerank" or client is None or not client.configured:
        return done(base, "bm25")

    # 只把 BM25 的前 top_n 名送去重排 —— 不對整頁做。
    # 一頁常有 20~200 節,兩階段能省下 6 倍以上的模型工作量。
    shortlist = [i for i in base.order if base.scores[i] > 0][:top_n]
    if not shortlist:
        return done(base, "bm25")

    docs = [sections[i].title + "\n" + sections[i].body for i in shortlist]
    try:
        local_order = await client.order(query or "", docs)
    except RerankUnavailable as exc:
        # 降級而非失敗:拿得到 BM25 的結果,總比整個請求掛掉好。
        log.warning("重排不可用,降級為 BM25: %s", exc)
        return done(base, "bm25", str(exc))

    reordered = [shortlist[i] for i in local_order if i < len(shortlist)]
    picked = set(reordered)
    rest = [i for i in base.order if i not in picked]

    # 重排後改用名次倒數當分數:順序與重排一致,且 select() 的「分數 > 0」
    # 條件仍然成立(它靠這個把 chrome 擋在外面)。
    scores = list(base.scores)
    for pos, idx in enumerate(reordered):
        scores[idx] = 1.0 / (pos + 1)

    return done(
        Ranking(order=reordered + rest, scores=scores, stats=base.stats, matched=True),
        "rerank",
    )
