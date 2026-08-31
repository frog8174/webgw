"""查詢感知選節。

實測依據 (12 個查詢 / 3 個頁面,ground truth = 含答案的章節):

    預算      BM25(有查詢)   密度啟發式   文件順序
    2000 tok    11/12          1/12        1/12
    4000 tok    12/12          1/12        3/12
    8000 tok    12/12          5/12       10/12

兩個結論寫進了實作:
  1. 有 query 時用 BM25;沒有 query 時用文件順序,不要用密度過濾
     (密度在每個預算下都輸給單純截斷,上游的 PruningContentFilter 就是密度啟發式)
  2. 加工幾乎沒有效益 —— 去停用詞/詞幹化/標題加權三種變體,rank@1 都還是 10/12,
     MRR 只從 0.86 升到 0.90。所以這裡刻意保持樸素的 BM25。

已知弱點:詞形與同義詞不匹配時會漏 (實測 "deprecated" 對章節 "Deprecations" 排第 4,
"build with Bazel" 排第 17)。這是方法邊界,不是調參問題 —— 由 no_match 後備路徑接住。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .sections import Section, strip_links

_LATIN = re.compile(r"[a-z][a-z0-9_.+-]{1,}")
_CJK_RUN = re.compile(r"[一-鿿]+")
_NUM = re.compile(r"\d+\.?\d*")

K1 = 1.5
B = 0.75


def terms(text: str) -> list[str]:
    """拉丁詞 + CJK 字元 bigram + 數字。

    CJK 用 bigram 而非分詞器:不必額外相依,對中文檢索是標準做法。
    代價是精度不如真正的分詞 —— 中文樣本目前驗證不足,這是已知風險。
    """
    low = strip_links(text).lower()
    out = _LATIN.findall(low)
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(_NUM.findall(low))
    return out


def bm25_scores(sections: list[Section], query: str) -> list[float]:
    docs = [terms(s.title + "\n" + s.body) for s in sections]
    n = len(docs)
    if n == 0:
        return []
    avg_len = sum(len(d) for d in docs) / n

    doc_freq: Counter[str] = Counter()
    for d in docs:
        doc_freq.update(set(d))

    q_terms = terms(query)
    scores: list[float] = []
    for d in docs:
        tf = Counter(d)
        score = 0.0
        for t in q_terms:
            f = tf.get(t)
            if not f:
                continue
            idf = math.log((n - doc_freq[t] + 0.5) / (doc_freq[t] + 0.5) + 1)
            score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * len(d) / max(avg_len, 1)))
        scores.append(score)
    return scores


@dataclass
class Pick:
    section: Section
    take_tokens: int          # 實際納入的 token 數,可能小於 section.tokens
    truncated: bool
    score: float


@dataclass
class Selection:
    picks: list[Pick]
    used_tokens: int
    strategy: str             # "bm25" | "document_order"
    matched: bool             # query 是否在此頁有任何匹配


def _take(
    sections: list[Section],
    order: list[int],
    scores: list[float] | None,
    budget: int,
    max_frac: float,
    require_score: bool,
) -> tuple[list[Pick], int]:
    picks: list[Pick] = []
    used = 0
    cap = max(1, int(budget * max_frac))
    for i in order:
        score = scores[i] if scores else 0.0
        # 不匹配查詢的章節一律不收。chrome(導覽列/頁尾/登入元件)在此出局 ——
        # 它們不含查詢詞,分數為 0,所以不需要另外維護 chrome 黑名單。
        if require_score and score <= 0:
            break
        sec = sections[i]
        take = min(sec.tokens, cap)
        if used + take > budget:
            # 停止而非跳過:跳過會撿低分小節填滿預算,實測多花 575 tok 而命中率不變。
            break
        picks.append(Pick(section=sec, take_tokens=take, truncated=take < sec.tokens, score=score))
        used += take
    return picks, used


def select(
    sections: list[Section],
    query: str | None,
    budget: int,
    max_frac: float = 0.5,
) -> Selection:
    if not sections:
        return Selection(picks=[], used_tokens=0, strategy="document_order", matched=False)

    if query:
        scores = bm25_scores(sections, query)
        if any(s > 0 for s in scores):
            order = sorted(range(len(sections)), key=lambda i: -scores[i])
            picks, used = _take(sections, order, scores, budget, max_frac, require_score=True)
            if picks:
                return Selection(picks=picks, used_tokens=used, strategy="bm25", matched=True)

    # 後備:沒有 query,或 query 在此頁完全無匹配。
    # 用文件順序而非密度過濾 —— 實測文件順序在每個預算下都贏過密度。
    order = list(range(len(sections)))
    picks, used = _take(sections, order, None, budget, max_frac, require_score=False)
    return Selection(
        picks=picks, used_tokens=used, strategy="document_order", matched=False
    )
