"""查詢感知選節。

分成兩個獨立的步驟,好讓 reranker 能插在中間:
    rank()    決定段落的順序(BM25)
    select()  依順序填滿 token 預算

實測依據 (30 個 ground-truth 案例:12 英文 + 18 中文):

    設定                   rank@1     額外成本
    BM25                   21/30        3ms
    BM25 + t2s             24/30      +145ms
    BM25 + t2s + 重排       28/30     +3000ms

t2s 修好的 3 個全是繁簡跨字集;重排再修好的 5 個全是「字面不匹配但語意相關」
(deprecated vs Deprecations、Bazel、訓練方式…),那是 BM25 的方法邊界,
任何正規化都補不了。

預算 8000 的依據:台積電那頁 (92k tokens) 實測 4000 ✗、6000 ✗、8000 ✓。
max_frac 0.35 的依據:0.5 時單一段落就吃掉半個預算,實測有一次只裝進 1 節。
"""
from __future__ import annotations

import functools
import math
import re
from collections import Counter
from dataclasses import dataclass

from .sections import Section, strip_links

_LATIN = re.compile(r"[a-z][a-z0-9_.+-]{1,}")
_CJK_RUN = re.compile(r"[一-鿿]+")
_HAS_CJK = re.compile(r"[一-鿿]")
_NUM = re.compile(r"\d+\.?\d*")

K1 = 1.5
B = 0.75


@functools.lru_cache(maxsize=1)
def _converter():
    """繁->簡轉換器。取不到就回 None,呼叫端據此略過正規化。

    正規化的用意跟英文檢索一律轉小寫相同:讓查詢和文件落在同一個字集,
    BM25 本身完全不用改。實測 12 個中文案例:改善 4、持平 8、變差 0。
    """
    try:
        from opencc import OpenCC

        return OpenCC("t2s")
    except Exception:  # noqa: BLE001
        return None


def normalize_script(text: str) -> str:
    """把繁體正規化為簡體。沒有 CJK 就原樣返回,省下轉換成本。"""
    if not _HAS_CJK.search(text):
        return text
    cc = _converter()
    return cc.convert(text) if cc is not None else text


def terms(text: str) -> list[str]:
    """拉丁詞 + CJK 字元 bigram + 數字。

    CJK 用 bigram 而非分詞器:不必額外相依,對中文檢索是標準做法。
    轉換前先做繁簡正規化 —— 否則繁體查詢對簡體內容會完全失效
    (實測:全頁 21 節只有「参考文献」偶然得分)。
    """
    low = normalize_script(strip_links(text)).lower()
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
class MatchStats:
    """查詢與頁面的匹配品質。

    取代舊的 query_matched 布林值 —— 那個只表示「有任何段落分數 > 0」,
    實測會誤導:跨字集時全頁只有「参考文献」偶然得 0.83 分,它照樣回 true,
    agent 會以為查詢命中了。
    """

    sections_total: int
    sections_scored: int
    top_score: float
    second_score: float

    @property
    def scored_ratio(self) -> float:
        return self.sections_scored / max(self.sections_total, 1)

    @property
    def score_gap(self) -> float:
        """最高分與次高分的比值。接近 1 表示分不出勝負。"""
        if self.second_score <= 0:
            return float("inf") if self.top_score > 0 else 0.0
        return self.top_score / self.second_score

    @property
    def confidence(self) -> str:
        if self.sections_scored == 0:
            return "none"
        if self.scored_ratio < 0.15 or self.top_score < 1.0:
            return "low"
        if self.score_gap < 1.5:
            return "medium"
        return "high"

    def as_dict(self) -> dict:
        return {
            "sections_total": self.sections_total,
            "sections_scored": self.sections_scored,
            "scored_ratio": round(self.scored_ratio, 3),
            "top_score": round(self.top_score, 2),
            "score_gap": round(self.score_gap, 2) if self.score_gap != float("inf") else None,
            "confidence": self.confidence,
        }


@dataclass
class Ranking:
    order: list[int]          # sections 的索引,由最相關排到最不相關
    scores: list[float]       # 與 sections 同序的原始分數
    stats: MatchStats
    matched: bool             # 是否有任何段落匹配到查詢


def rank(sections: list[Section], query: str | None) -> Ranking:
    """依查詢排序段落。沒有 query 或完全無匹配時退回文件順序。

    退回文件順序而非密度過濾,是實測結論:密度啟發式在每個預算下都輸給
    單純按順序截斷(2k 預算 1/12 vs 1/12,8k 預算 5/12 vs 10/12)。
    """
    n = len(sections)
    empty = MatchStats(n, 0, 0.0, 0.0)
    if n == 0:
        return Ranking([], [], empty, False)
    if not query:
        return Ranking(list(range(n)), [0.0] * n, empty, False)

    scores = bm25_scores(sections, query)
    ordered = sorted(range(n), key=lambda i: -scores[i])
    positive = [s for s in scores if s > 0]
    if not positive:
        return Ranking(list(range(n)), scores, empty, False)

    top = scores[ordered[0]]
    second = scores[ordered[1]] if n > 1 else 0.0
    stats = MatchStats(n, len(positive), top, second)
    return Ranking(ordered, scores, stats, True)


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
    strategy: str             # "bm25" | "rerank" | "document_order"
    matched: bool
    stats: MatchStats


def select(
    sections: list[Section],
    ranking: Ranking,
    budget: int,
    max_frac: float = 0.35,
    strategy: str = "bm25",
) -> Selection:
    """依既定順序填滿預算。

    停止而非跳過:跳過裝不下的段落、繼續撿小的來填,實測多花 575 tokens
    而命中率完全不變 —— 那些是純填充物。

    單節上限 = budget * max_frac:防止超大段落炸掉預算(實測 OpenReview 有
    21,146 tok 的單一章節,舊實作會把 4,000 的預算撐到 5.3 倍)。
    """
    picks: list[Pick] = []
    used = 0
    cap = max(1, int(budget * max_frac))
    require_score = ranking.matched and strategy != "document_order"

    for i in ranking.order:
        score = ranking.scores[i] if i < len(ranking.scores) else 0.0
        # 不匹配查詢的段落一律不收。導覽列、頁尾、登入元件不含查詢詞,
        # 分數為 0 自然出局 —— 不需要維護 chrome 黑名單。
        if require_score and score <= 0:
            break
        sec = sections[i]
        take = min(sec.tokens, cap)
        if used + take > budget:
            break
        picks.append(Pick(sec, take, take < sec.tokens, score))
        used += take

    return Selection(
        picks=picks,
        used_tokens=used,
        strategy=strategy if ranking.matched else "document_order",
        matched=ranking.matched,
        stats=ranking.stats,
    )
