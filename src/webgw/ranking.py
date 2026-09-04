"""Query-aware section selection.

Split into two independent steps so a reranker can sit between them:
    rank()    decides section order (BM25)
    select()  fills the token budget in that order

Measured on 30 ground-truth cases (12 English + 18 Chinese):

    configuration                     rank@1     added cost
    BM25                              21/30        3 ms
    BM25 + script normalization       24/30      +145 ms
    BM25 + normalization + rerank     28/30    +3,000 ms

The 3 cases normalization fixed were all Traditional/Simplified script
mismatches; the 5 the reranker fixed on top of that were all "no literal
overlap but semantically related" (deprecated vs Deprecations, Bazel, training
method, ...), which is the boundary of the method itself -- no amount of
normalization reaches them.

Budget 8000 comes from measurement: on a 92k-token page, 4000 failed, 6000
failed, 8000 succeeded. max_frac 0.35 likewise: at 0.5 a single section ate
half the budget and one page fit only 1 section.
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
    """Traditional-to-Simplified converter, or None if unavailable.

    Callers skip normalization when this returns None. The purpose is the same
    as lowercasing in English retrieval: put query and document in one script so
    BM25 itself needs no changes. Measured across 12 Chinese cases: 4 improved,
    8 unchanged, 0 regressed.
    """
    try:
        from opencc import OpenCC

        return OpenCC("t2s")
    except Exception:  # noqa: BLE001
        return None


def normalize_script(text: str) -> str:
    """Normalize Traditional Chinese to Simplified.

    Returns the text unchanged when it holds no CJK, saving the conversion cost.
    """
    if not _HAS_CJK.search(text):
        return text
    cc = _converter()
    return cc.convert(text) if cc is not None else text


def terms(text: str) -> list[str]:
    """Latin words, CJK character bigrams, and numbers.

    CJK uses bigrams rather than a word segmenter: no extra dependency, and it
    is the standard approach for Chinese retrieval. Script normalization runs
    first -- without it a Traditional query against Simplified content fails
    almost completely (measured: of 21 sections on a page, only the references
    heading scored at all, by coincidence).
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
    """How well the query matched the page.

    Replaces an earlier `query_matched` boolean, which only meant "some section
    scored above zero" and was measured to mislead: across a script mismatch the
    only section scoring anything was the references heading, at 0.83, and the
    boolean still reported true -- so the agent believed the query had hit.
    """

    sections_total: int
    sections_scored: int
    top_score: float
    second_score: float
    # Which ranker produced these scores. The two scales are unrelated, so
    # confidence thresholds cannot be shared:
    #   bm25   unbounded; relevant sections measured in the 5-30 range
    #   rerank sigmoid output 0-1; relevant >0.99, irrelevant <0.001
    source: str = "bm25"

    @property
    def scored_ratio(self) -> float:
        return self.sections_scored / max(self.sections_total, 1)

    @property
    def score_gap(self) -> float:
        """Top score over runner-up. Near 1 means the ranking cannot separate them."""
        if self.second_score <= 0:
            return float("inf") if self.top_score > 0 else 0.0
        return self.top_score / self.second_score

    @property
    def confidence(self) -> str:
        if self.sections_scored == 0:
            return "none"
        if self.source == "rerank":
            # Cross-encoder scores are sigmoid outputs, so the absolute value is
            # the meaningful signal -- score gaps on this scale routinely run
            # into the thousands and lose all discriminating power.
            if self.top_score >= 0.5:
                return "high"
            if self.top_score >= 0.1:
                return "medium"
            return "low"
        if self.scored_ratio < 0.15 or self.top_score < 1.0:
            return "low"
        if self.score_gap < 1.5:
            return "medium"
        return "high"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "sections_total": self.sections_total,
            "sections_scored": self.sections_scored,
            "scored_ratio": round(self.scored_ratio, 3),
            "top_score": round(self.top_score, 2),
            "score_gap": round(self.score_gap, 2) if self.score_gap != float("inf") else None,
            "confidence": self.confidence,
        }


@dataclass
class Ranking:
    order: list[int]          # section indices, most relevant first
    scores: list[float]       # raw scores, parallel to sections
    stats: MatchStats
    matched: bool             # whether any section matched the query


def rank(sections: list[Section], query: str | None) -> Ranking:
    """Order sections by query, falling back to document order.

    The fallback is document order rather than density filtering, and that is a
    measured conclusion: density heuristics lost to plain truncation at every
    budget (1/12 vs 1/12 at a 2k budget, 5/12 vs 10/12 at 8k).
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
    take_tokens: int          # tokens actually taken, may be less than section.tokens
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
    """Fill the budget following the given order.

    Stop rather than skip: skipping an oversized section to keep picking smaller
    ones was measured spending 575 extra tokens with no change in hit rate --
    what it picked up was pure filler.

    Per-section cap = budget * max_frac, so one huge section cannot blow the
    budget. Measured case: OpenReview had a single 21,146-token chapter, and the
    earlier implementation stretched a 4,000 budget to 5.3x.
    """
    picks: list[Pick] = []
    used = 0
    cap = max(1, int(budget * max_frac))
    require_score = ranking.matched and strategy != "document_order"

    for i in ranking.order:
        score = ranking.scores[i] if i < len(ranking.scores) else 0.0
        # Never take sections that do not match the query. Navigation bars,
        # footers and login widgets contain no query terms, so they score zero
        # and drop out on their own -- no chrome blocklist to maintain.
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
