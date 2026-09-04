"""Retrieval mode switching and degradation.

Measured over 30 ground-truth cases: bm25 rank@1 24/30, rerank 28/30. Every
case reranking additionally fixed was "no literal overlap but semantically
related", which is the boundary of the BM25 method.
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
    """Fake reranking service with switchable behaviour."""

    def __init__(self, order=None, fail: str | None = None):
        self._order = order
        self._fail = fail
        self.calls = 0
        self.last_docs: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def order(self, query, documents):
        """Return [(index, score), ...] -- the same shape as the real client.

        The scores must come back too, or the caller can only reuse BM25's
        statistics and match.confidence ends up describing how certain BM25 was
        rather than the reranker.
        """
        self.calls += 1
        self.last_docs = documents
        if self._fail:
            raise RerankUnavailable(self._fail)
        order = self._order if self._order is not None else list(range(len(documents)))
        # Decreasing synthetic scores, so the top result scores highest.
        return [(i, 1.0 - n * 0.1) for n, i in enumerate(order)]


# -- mode selection ----------------------------------------------------------
def test_mode_normalisation():
    assert retrieval.normalize_mode(None) == "bm25"
    assert retrieval.normalize_mode("RERANK") == "rerank"
    assert retrieval.normalize_mode("nonsense") == "bm25"      # invalid falls back to default


async def test_bm25_mode_never_calls_reranker(page):
    rr = StubReranker()
    out = await retrieval.retrieve(page, "deprecated api", "bm25", rr)
    assert out.mode_used == "bm25"
    assert rr.calls == 0


async def test_rerank_mode_reorders():
    """Reranking promotes the section BM25 placed 2nd to 1st.

    The query has to match several sections for the shortlist to hold anything
    -- with a single candidate reranking cannot change any result.
    """
    secs = [
        _sec("s0", "Install", "install the api package with pip " * 25, 0),
        _sec("s1", "Deprecations", "the api package removes old install paths " * 25, 1),
    ]
    rr = StubReranker(order=[1, 0])
    out = await retrieval.retrieve(secs, "api package install", "rerank", rr)
    assert out.mode_used == "rerank"
    assert rr.calls == 1
    assert len(rr.last_docs) == 2, "both sections should enter the shortlist"
    assert secs[out.ranking.order[0]].id == "s1"


async def test_rerank_failure_degrades_to_bm25(page):
    """When reranking fails, degrade instead of failing the whole request --
    BM25 results beat returning nothing."""
    rr = StubReranker(fail="timeout (15.0s)")
    out = await retrieval.retrieve(page, "deprecated api", "rerank", rr)
    assert out.mode_used == "bm25"
    assert out.mode_requested == "rerank"
    assert out.degraded and "timeout" in out.degraded
    assert out.ranking.order          # still a usable ordering


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
    """With nothing matching at all, there is no point spending a model call."""
    rr = StubReranker()
    out = await retrieval.retrieve(page, "zzzz qqqq", "rerank", rr)
    assert out.mode_used == "document_order"
    assert rr.calls == 0


# -- two-stage: only the shortlist is sent ------------------------------------
async def test_only_shortlist_is_sent_to_reranker():
    """The whole page is never reranked -- pages commonly hold 20-200 sections,
    so two stages save 6x or more model work."""
    secs = [_sec(f"s{i}", f"Section {i}", "deprecated api removed " * 20, i)
            for i in range(50)]
    rr = StubReranker()
    await retrieval.retrieve(secs, "deprecated api", "rerank", rr, top_n=10)
    assert len(rr.last_docs) == 10


async def test_sections_outside_shortlist_are_kept_after(page):
    """Sections that missed the shortlist stay behind it and must not be lost."""
    rr = StubReranker(order=[0])
    out = await retrieval.retrieve(page, "deprecated api", "rerank", rr, top_n=1)
    assert len(out.ranking.order) == len(page)


# -- match stats must reflect the ranker actually used ------------------------
async def test_rerank_stats_come_from_reranker_not_bm25():
    """Reusing BM25's statistics makes both modes report identical match numbers
    while selecting different sections -- an agent seeing confidence: high would
    read it as the reranker being certain when it is BM25's confidence.
    Confirmed misleading by measurement.
    """
    secs = [
        _sec("s0", "Install", "install the api package with pip " * 25, 0),
        _sec("s1", "Deprecations", "the api package removes old install paths " * 25, 1),
    ]
    bm = await retrieval.retrieve(secs, "api package install", "bm25", StubReranker())
    rr = await retrieval.retrieve(secs, "api package install", "rerank",
                                  StubReranker(order=[1, 0]))
    assert bm.ranking.stats.source == "bm25"
    assert rr.ranking.stats.source == "rerank"
    assert rr.ranking.stats.top_score != bm.ranking.stats.top_score


async def test_rerank_confidence_uses_sigmoid_thresholds():
    """Cross-encoder scores are 0-1 sigmoid outputs while BM25 is unbounded, so
    the thresholds cannot be shared.

    Applying BM25's threshold (top_score < 1.0 means low) to reranker scores
    would misread 0.99 -- an extremely strong match -- as low.
    """
    from webgw.ranking import MatchStats

    assert MatchStats(10, 5, 0.99, 0.001, source="rerank").confidence == "high"
    assert MatchStats(10, 5, 0.99, 0.001, source="bm25").confidence == "low"


# -- reranker authentication: self-hosted vs commercial API -------------------
def test_selfhosted_reranker_sends_no_auth_header():
    """Self-hosted vLLM usually has no auth, so sending an Authorization header
    would be meaningless."""
    from webgw.reranker import RerankClient

    c = RerankClient("http://bge-reranker:8000", "bge-reranker-v2-m3")
    assert c._headers() == {"Content-Type": "application/json"}


def test_commercial_reranker_sends_bearer_key():
    """Commercial services (Cohere, Jina, Voyage) share the request shape and
    differ only in authentication.

    Without this header the identical payload comes back 401, the service is
    unreachable, and mode="rerank" silently degrades to bm25 -- which makes a
    missing key hard to spot.
    """
    from webgw.reranker import RerankClient

    c = RerankClient("https://api.jina.ai", "jina-reranker-v2-base-multilingual",
                     api_key="secret-key")
    assert c._headers()["Authorization"] == "Bearer secret-key"
    assert c._headers()["Content-Type"] == "application/json"
