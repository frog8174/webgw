"""選節演算法的回歸測試。固定住 2026-08-30 驗證出來的行為。"""
from __future__ import annotations

import pytest

from webgw import ranking
from webgw.sections import Section


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


def test_bm25_ranks_relevant_section_first(page):
    sel = ranking.select(page, "which optimizer was used", budget=4000)
    assert sel.strategy == "bm25"
    assert sel.matched is True
    assert sel.picks[0].section.id == "s1"


def test_chrome_excluded_without_blocklist(page):
    """導覽列/頁尾不含查詢詞,分數為 0,不需維護 chrome 黑名單就會出局。"""
    sel = ranking.select(page, "optimizer adam warmup", budget=4000)
    picked = {p.section.id for p in sel.picks}
    assert "s0" not in picked and "s3" not in picked


def test_no_query_falls_back_to_document_order(page):
    """沒有 query 時用文件順序 —— 實測密度啟發式在每個預算下都輸給它。"""
    sel = ranking.select(page, None, budget=4000)
    assert sel.strategy == "document_order"
    assert sel.matched is False
    assert [p.section.id for p in sel.picks] == ["s0", "s1", "s2", "s3"]


def test_unmatched_query_falls_back(page):
    sel = ranking.select(page, "zzzz qqqq nonexistent", budget=4000)
    assert sel.strategy == "document_order"
    assert sel.matched is False


def test_oversized_section_is_capped_not_skipped():
    """單節超過預算時要裁切而非整節收下。

    實測 OpenReview 有 21,146 tok 的單一章節,舊實作會把 4,000 的預算撐到 21,146 (5.3x)。
    """
    huge = _sec("s1", "Active Venues", "venue submission deadline " * 4000, 0)
    sel = ranking.select([huge], "venue submission", budget=4000, max_frac=0.5)
    assert sel.used_tokens <= 4000
    assert sel.picks[0].truncated is True


def test_cjk_bigram_tokenisation():
    t = ranking.terms("麒麟 9030 Pro 製程代差")
    assert "麒麟" in t and "製程" in t and "9030" in t and "pro" in t


def test_empty_sections():
    sel = ranking.select([], "anything", budget=4000)
    assert sel.picks == [] and sel.used_tokens == 0


def test_small_but_real_page_is_not_empty_content():
    """example.com 只有 165 字元但是合法頁面,不可誤判為 empty_content。

    對照:Reuters 被 DataDome 擋時 raw_markdown 只有 1 個字元。
    """
    from webgw import outcome

    result = {"success": True, "status_code": 200}
    assert outcome.classify(result, "x" * 165).code == outcome.OK
    assert outcome.classify(result, "\n").code == outcome.EMPTY_CONTENT
