"""選節演算法。固定住實測出來的行為。

實測依據 (30 個 ground-truth 案例):
    BM25            rank@1 21/30
    BM25 + t2s      rank@1 24/30      +145ms
    再加上重排        rank@1 28/30     +3000ms
"""
from __future__ import annotations

import pytest

from webgw import ranking
from webgw.ranking import Section, rank, select


def _sec(sid: str, title: str, body: str, pos: int) -> Section:
    from webgw import tokens

    return Section(
        id=sid, level=2, title=title, body=body, position=pos, tokens=tokens.count(body)
    )


@pytest.fixture
def page() -> list[Section]:
    return [
        _sec("s0", "Navigation Menu", "Sign in Sign up Search Home About " * 20, 0),
        _sec("s1", "Optimizer", "We used the Adam optimizer with beta1 0.9 and warmup steps. " * 20, 1),
        _sec("s2", "Results", "BLEU score of 28.4 on English to German newstest2014. " * 20, 2),
        _sec("s3", "Footer", "Copyright terms privacy cookies contact " * 20, 3),
    ]


def _pick(page, query, budget=8000, max_frac=0.35):
    return select(page, rank(page, query), budget, max_frac)


# ── 排序 ──────────────────────────────────────────────────────────
def test_relevant_section_ranks_first(page):
    sel = _pick(page, "which optimizer was used")
    assert sel.strategy == "bm25"
    assert sel.picks[0].section.id == "s1"


def test_chrome_excluded_without_blocklist(page):
    """導覽列/頁尾不含查詢詞,分數為 0,不需維護 chrome 黑名單就會出局。"""
    sel = _pick(page, "optimizer adam warmup")
    picked = {p.section.id for p in sel.picks}
    assert "s0" not in picked and "s3" not in picked


def test_no_query_falls_back_to_document_order(page):
    """沒有 query 時用文件順序 —— 實測密度啟發式在每個預算下都輸給它。"""
    sel = _pick(page, None)
    assert sel.strategy == "document_order"
    assert [p.section.id for p in sel.picks] == ["s0", "s1", "s2", "s3"]


def test_unmatched_query_falls_back(page):
    sel = _pick(page, "zzzz qqqq nonexistent")
    assert sel.strategy == "document_order"
    assert sel.matched is False


def test_oversized_section_is_capped_not_skipped():
    """單節超過預算時要裁切而非整節收下。

    實測 OpenReview 有 21,146 tok 的單一章節,舊實作會把 4,000 的預算撐到 5.3 倍。
    """
    huge = _sec("s1", "Active Venues", "venue submission deadline " * 4000, 0)
    sel = select([huge], rank([huge], "venue submission"), 8000, 0.35)
    assert sel.used_tokens <= 8000
    assert sel.picks[0].truncated is True


def test_empty_sections():
    sel = select([], rank([], "anything"), 8000)
    assert sel.picks == [] and sel.used_tokens == 0


# ── 繁簡正規化 ────────────────────────────────────────────────────
def test_traditional_and_simplified_produce_same_terms():
    """繁簡正規化的用意等同英文檢索一律轉小寫:讓兩邊落在同一個字集。

    沒有它,繁體查詢對簡體內容會完全失效 —— 實測全頁 21 節只有「参考文献」
    偶然得分,選出來的是一整頁參考文獻條目。
    """
    if ranking._converter() is None:
        pytest.skip("opencc 不可用")
    assert set(ranking.terms("編碼器與解碼器")) == set(ranking.terms("编码器与解码器"))
    assert set(ranking.terms("訓練")) == set(ranking.terms("训练"))


def test_cross_script_query_matches_simplified_content():
    if ranking._converter() is None:
        pytest.skip("opencc 不可用")
    secs = [
        _sec("s0", "参考文献", "Vaswani Attention Is All You Need 2017 " * 40, 0),
        _sec("s1", "编码器-解码器架构", "编码器由六个相同的层堆叠而成,解码器同样由六层组成。" * 20, 1),
    ]
    sel = _pick(secs, "編碼器 解碼器 架構")      # 繁體查詢
    assert sel.picks[0].section.id == "s1"


def test_latin_text_skips_conversion():
    """沒有 CJK 就不做轉換,省下成本(90k 頁面實測轉換要 145ms)。"""
    assert ranking.normalize_script("hello world") == "hello world"


# ── 匹配訊號 ──────────────────────────────────────────────────────
def test_match_stats_reports_breadth_not_just_boolean(page):
    """取代舊的 query_matched 布林值。

    舊欄位只表示「有任何段落分數 > 0」—— 跨字集時全頁只有一節偶然得分
    它照樣回 true,agent 會以為查詢命中了。
    """
    r = rank(page, "optimizer adam warmup")
    assert r.stats.sections_scored >= 1
    assert r.stats.sections_total == 4
    assert 0 < r.stats.scored_ratio <= 1
    assert r.stats.confidence in ("none", "low", "medium", "high")


def test_no_match_reports_zero_confidence(page):
    r = rank(page, "zzzz qqqq nonexistent")
    assert r.stats.sections_scored == 0
    assert r.stats.confidence == "none"


def test_stats_serialisable(page):
    d = rank(page, "optimizer").stats.as_dict()
    assert set(d) >= {"sections_total", "sections_scored", "confidence", "top_score"}
