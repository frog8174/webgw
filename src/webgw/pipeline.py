"""Orchestration: admission -> cache -> fetch -> classify -> store -> split ->
retrieve -> assemble response."""
from __future__ import annotations

from datetime import datetime, timezone

from . import admission, outcome as oc, ranking, retrieval, sections, tokens
from .cache import CacheStore
from .config import Config
from .crawl_client import CrawlClient
from .limits import ConcurrencyLimiter
from .reranker import RerankClient


def _error_envelope(url: str, code: str, detail: str) -> dict:
    o = oc.Outcome(code, detail)
    return {
        "outcome": code,
        "detail": detail,
        "retryable": o.retryable,
        "hint": o.hint,
        "url": url,
        "content": None,
    }


async def _render(
    url: str,
    final_url: str,
    title: str,
    status_code: int | None,
    markdown: str,
    query: str | None,
    mode: str,
    cfg: Config,
    reranker: RerankClient | None,
    *,
    cache_state: str,
    age_s: int | None = None,
    degraded_from: str | None = None,
) -> dict:
    """Assemble raw markdown into the response envelope.

    Cache hits and fresh fetches share this path -- selection is always
    recomputed from raw, so re-querying the same page with a different query (or
    a different mode) never needs another crawl. That is exactly why raw is
    stored rather than fit.
    """
    raw_tokens = tokens.count(markdown)
    env: dict = {
        "outcome": oc.OK,
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "title": title,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw_tokens": raw_tokens,
        "cache": cache_state,
    }
    if age_s is not None:
        env["cache_age_s"] = age_s
    if cache_state == "stale":
        env["note"] = (
            f"Re-fetch failed{f' ({degraded_from})' if degraded_from else ''}; "
            f"returning cached content from {age_s} seconds ago. It may be out of date."
        )

    # Small pages are returned whole. The tokens saved are not worth the risk of
    # selecting the wrong part.
    if raw_tokens <= cfg.passthrough_max_tokens:
        env.update({
            "mode": "passthrough",
            "returned_tokens": raw_tokens,
            "truncated": False,
            "content": markdown,
        })
        return env

    secs = sections.split(markdown)
    if not secs:
        text = tokens.truncate(markdown, cfg.select_budget_tokens)
        env.update({
            "mode": "truncate",
            "returned_tokens": tokens.count(text),
            "truncated": True,
            "content": text,
            "note": env.get("note")
            or "The page has no heading structure; truncated in document order.",
        })
        return env

    result = await retrieval.retrieve(
        secs, query, mode, reranker, top_n=cfg.rerank_top_n
    )
    sel = ranking.select(
        secs, result.ranking, cfg.select_budget_tokens, cfg.max_section_frac,
        strategy=result.mode_used,
    )
    picked = {p.section.id for p in sel.picks}

    env.update({
        "mode": sel.strategy,
        "retrieval": result.as_dict(),
        # Replaces an earlier query_matched boolean, which only meant "some
        # section scored above zero". Across a script mismatch, where the only
        # section scoring anything was the references heading, it still reported
        # true -- misleading the agent.
        "match": sel.stats.as_dict(),
        "returned_tokens": sel.used_tokens,
        "truncated": sel.used_tokens < raw_tokens,
        "excerpts": [
            {
                "section_id": p.section.id,
                "title": p.section.display_title,
                "level": p.section.level,
                "tokens": p.take_tokens,
                "truncated": p.truncated,
                "text": tokens.truncate(p.section.body, p.take_tokens),
            }
            for p in sel.picks
        ],
        "outline_omitted": [
            s.as_outline_entry() for s in secs if s.id not in picked
        ][:40],
    })
    if not sel.matched and query and not env.get("note"):
        env["note"] = (
            "The query matched nothing on this page, so it was truncated in document "
            "order. This may be the wrong page."
        )
    elif sel.stats.confidence in ("low", "none") and not env.get("note"):
        env["note"] = (
            "The query matches this page only weakly. If the returned sections do not "
            'contain what you need, retry with mode="rerank" -- the content is cached, '
            "so it will not be crawled again."
        )
    return env


async def fetch(
    url: str,
    query: str | None,
    cfg: Config,
    client: CrawlClient,
    cache: CacheStore | None = None,
    limiter: ConcurrencyLimiter | None = None,
    reranker: RerankClient | None = None,
    mode: str | None = None,
) -> dict:
    mode = retrieval.normalize_mode(mode or cfg.retrieval_mode)

    verdict = admission.check(url)
    if not verdict.allowed:
        return _error_envelope(url, verdict.reason, verdict.detail)

    cached = await cache.get(url) if cache else None
    if cached is not None and cached.is_fresh(cache.max_age_for(url)):
        return await _render(
            url, cached.final_url, cached.title, cached.status_code, cached.markdown,
            query, mode, cfg, reranker, cache_state="hit", age_s=cached.age_s,
        )

    # Concurrency ceiling: queue rather than reject. Fetching is slow anyway, so
    # waiting beats failing outright.
    if limiter is not None:
        async with limiter:
            crawled = await client.fetch(url)
    else:
        crawled = await client.fetch(url)

    if not crawled.ok:
        # The fetch failed but an in-retention copy is on hand -- serve it and
        # mark it stale.
        if cached is not None:
            return await _render(
                url, cached.final_url, cached.title, cached.status_code, cached.markdown,
                query, mode, cfg, reranker, cache_state="stale", age_s=cached.age_s,
                degraded_from=oc.FETCH_FAILED,
            )
        code = oc.TIMEOUT if crawled.transport_error == "timeout" else oc.FETCH_FAILED
        return _error_envelope(url, code, crawled.transport_error)

    result = crawled.result
    markdown = CrawlClient.raw_markdown(result)

    final_url = result.get("redirected_url") or url
    redirect_verdict = admission.check_redirect(url, final_url)
    if not redirect_verdict.allowed:
        # Discard the content and do not fall back to cache -- this is a
        # security event, not a transient failure.
        return _error_envelope(url, redirect_verdict.reason, redirect_verdict.detail)

    status = oc.classify(result, markdown)
    if not status.ok:
        if cached is not None:
            return await _render(
                url, cached.final_url, cached.title, cached.status_code, cached.markdown,
                query, mode, cfg, reranker, cache_state="stale", age_s=cached.age_s,
                degraded_from=status.code,
            )
        env = _error_envelope(url, status.code, status.detail)
        env["final_url"] = final_url
        env["status_code"] = result.get("status_code")
        return env

    title = (result.get("metadata") or {}).get("title") or ""
    if cache is not None:
        await cache.put(
            url,
            final_url=final_url,
            title=title,
            markdown=markdown,
            raw_tokens=tokens.count(markdown),
            status_code=result.get("status_code"),
        )

    return await _render(
        url, final_url, title, result.get("status_code"), markdown,
        query, mode, cfg, reranker, cache_state="miss",
    )
