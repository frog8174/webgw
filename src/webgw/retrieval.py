"""Retrieval mode orchestration: whether to rerank, and how to degrade.

Two modes:
    bm25    BM25 plus script normalization. About 2.5 seconds. Default.
    rerank  BM25 top-N shortlist -> cross-encoder rerank. About 5-8 seconds.

Measured on 30 ground-truth cases:
    bm25    rank@1 24/30
    rerank  rank@1 28/30

-- Why there is no auto mode ----------------------------------------------

The intent was "rerank only when BM25 is unsure", but the experiment ruled it
out:

  signal                       correct median   wrong median   overlaps
  share of scored sections            0.62           0.51        yes
  top score                          12.23           9.28        yes
  score gap                           1.64           1.46        yes
  query term coverage                 0.88           0.94        yes
  top-3 concentration                 0.52           0.43        yes
  total sections                     22.50          95.50        yes

All six signals overlap. The sharpest counterexample: "can I still build with
Bazel" had a score gap of 2.09, confidence high, and query term coverage of
1.00 -- and BM25 ranked the answer 19th.

A BM25 score says how *certain* the ranking is, not whether it is *right*, and
being confidently wrong is exactly the case worth catching. Page size fails as
a threshold too: >= 20 sections fires on 87% of pages (effectively always on),
while >= 30 drops to 25/30 (one better than plain BM25). Using "does the full
text fit the budget" is worse still -- only 1 of 30 pages fits, so it fires 97%
of the time.

So the decision belongs to the caller. The agent knows things this layer does
not: whether the question matters, whether a first attempt already missed, and
whether it can afford to wait. Raw content is cached, so retrying the same page
with mode="rerank" costs only the rerank seconds, not another crawl.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .ranking import MatchStats, Ranking, rank
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
    degraded: str | None      # set when rerank was wanted but failed and BM25 was used
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

    # Only BM25's top_n go to the reranker -- never the whole page. Pages
    # commonly hold 20-200 sections, so two-stage saves 6x or more model work.
    shortlist = [i for i in base.order if base.scores[i] > 0][:top_n]
    if not shortlist:
        return done(base, "bm25")

    docs = [sections[i].title + "\n" + sections[i].body for i in shortlist]
    try:
        local_scored = await client.order(query or "", docs)
    except RerankUnavailable as exc:
        # Degrade rather than fail: BM25 results beat losing the whole request.
        log.warning("reranker unavailable, falling back to BM25: %s", exc)
        return done(base, "bm25", str(exc))

    reordered: list[int] = []
    rr_scores: list[float] = []
    for local_i, score in local_scored:
        if local_i < len(shortlist):
            reordered.append(shortlist[local_i])
            rr_scores.append(score)
    picked = set(reordered)
    rest = [i for i in base.order if i not in picked]

    # Use the reranker's own scores rather than reciprocal rank -- reciprocal
    # rank discards "how relevant is this section", which is precisely what
    # match.confidence is meant to express.
    #
    # Sections scoring 0 (padding for rows the service did not return) get a
    # vanishing decreasing positive value so select()'s "score > 0" test still
    # holds -- that test is what keeps page chrome out.
    scores = [0.0] * len(sections)
    for pos, (idx, sc) in enumerate(zip(reordered, rr_scores)):
        scores[idx] = max(sc, 1e-9 / (pos + 1))

    # Rebuild stats from the reranker's scores. Reusing BM25's stats made both
    # modes report identical match numbers while selecting different sections --
    # confirmed by measurement as actively misleading.
    stats = MatchStats(
        sections_total=len(sections),
        sections_scored=sum(1 for sc in rr_scores if sc > 0),
        top_score=rr_scores[0] if rr_scores else 0.0,
        second_score=rr_scores[1] if len(rr_scores) > 1 else 0.0,
        source="rerank",
    )
    return done(
        Ranking(order=reordered + rest, scores=scores, stats=stats, matched=True),
        "rerank",
    )
