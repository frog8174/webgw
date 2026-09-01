"""檢索模式切換與降級。

實測 (30 個 ground-truth 案例):bm25 rank@1 24/30、rerank 28/30。
重排多修好的都是「字面不匹配但語意相關」的案例,那是 BM25 的方法邊界。
"""
from __future__ import annotations

import pytest

from webgw import retrieval, tokens
from webgw.reranker import RerankUnavailable
from webgw.sections import Section


def _sec(sid: str, title: str, body: str, pos: int) -> Section:
    return Section(id=sid, level=2, title=title, body=body, position=pos,
                   tokens=tokens.count(body))


@pytest.fixture
def page() -> list[Section]:
    return [
        _sec("s0", "Deprecations", "The old API is removed in this release. " * 30, 0),
        _sec("s1", "Highlights", "New features include faster compilation. " * 30, 1),
        _sec("s2", "Install", "Use pip install to get the package. " * 30, 2),
    ]


class StubReranker:
    """可切換行為的假重排服務。"""

    def __init__(self, order=None, fail: str | None = None):
        self._order = order
        self._fail = fail
        self.calls = 0
        self.last_docs: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def order(self, query, documents):
        self.calls += 1
        self.last_docs = documents
        if self._fail:
            raise RerankUnavailable(self._fail)
        return self._order if self._order is not None else list(range(len(documents)))


# ── 模式選擇 ──────────────────────────────────────────────────────
def test_mode_normalisation():
    assert retrieval.normalize_mode(None) == "bm25"
    assert retrieval.normalize_mode("RERANK") == "rerank"
    assert retrieval.normalize_mode("亂寫") == "bm25"      # 無效值退回預設


async def test_bm25_mode_never_calls_reranker(page):
    rr = StubReranker()
    out = await retrieval.retrieve(page, "deprecated api", "bm25", rr)
    assert out.mode_used == "bm25"
    assert rr.calls == 0


async def test_rerank_mode_reorders():
    """重排把 BM25 排第 2 的段落提到第 1。

    查詢要能匹配多個段落,shortlist 才有東西可重排 —— 只有一個候選時
    重排不會改變任何結果。
    """
    secs = [
        _sec("s0", "Install", "install the api package with pip " * 25, 0),
        _sec("s1", "Deprecations", "the api package removes old install paths " * 25, 1),
    ]
    rr = StubReranker(order=[1, 0])
    out = await retrieval.retrieve(secs, "api package install", "rerank", rr)
    assert out.mode_used == "rerank"
    assert rr.calls == 1
    assert len(rr.last_docs) == 2, "兩節都該進入 shortlist"
    assert secs[out.ranking.order[0]].id == "s1"


async def test_rerank_failure_degrades_to_bm25(page):
    """重排掛掉時降級而非整個請求失敗 —— 拿得到 BM25 的結果總比什麼都沒有好。"""
    rr = StubReranker(fail="逾時 (15.0s)")
    out = await retrieval.retrieve(page, "deprecated api", "rerank", rr)
    assert out.mode_used == "bm25"
    assert out.mode_requested == "rerank"
    assert out.degraded and "逾時" in out.degraded
    assert out.ranking.order          # 仍然有可用的排序


async def test_degradation_is_visible_in_output(page):
    rr = StubReranker(fail="HTTP 503")
    out = await retrieval.retrieve(page, "deprecated api", "rerank", rr)
    d = out.as_dict()
    assert d["mode"] == "bm25" and d["requested"] == "rerank"
    assert "degraded" in d


async def test_no_reranker_configured_stays_bm25(page):
    out = await retrieval.retrieve(page, "deprecated api", "rerank", None)
    assert out.mode_used == "bm25" and out.degraded is None


async def test_unmatched_query_skips_rerank(page):
    """完全沒匹配時不必浪費一次模型呼叫。"""
    rr = StubReranker()
    out = await retrieval.retrieve(page, "zzzz qqqq", "rerank", rr)
    assert out.mode_used == "document_order"
    assert rr.calls == 0


# ── 兩階段:只送前 N 名 ────────────────────────────────────────────
async def test_only_shortlist_is_sent_to_reranker():
    """不對整頁重排 —— 一頁常有 20~200 節,兩階段省下 6 倍以上的模型工作量。"""
    secs = [_sec(f"s{i}", f"Section {i}", "deprecated api removed " * 20, i)
            for i in range(50)]
    rr = StubReranker()
    await retrieval.retrieve(secs, "deprecated api", "rerank", rr, top_n=10)
    assert len(rr.last_docs) == 10


async def test_sections_outside_shortlist_are_kept_after(page):
    """未進入 shortlist 的段落要留在後面,不能遺失。"""
    rr = StubReranker(order=[0])
    out = await retrieval.retrieve(page, "deprecated api", "rerank", rr, top_n=1)
    assert len(out.ranking.order) == len(page)
