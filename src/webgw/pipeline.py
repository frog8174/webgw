"""編排:准入 -> 快取 -> 抓取 -> 判定 -> 存快取 -> 切節 -> 選節 -> 組裝回應。"""
from __future__ import annotations

from datetime import datetime, timezone

from . import admission, outcome as oc, ranking, sections, tokens
from .cache import CacheStore
from .config import Config
from .crawl_client import CrawlClient
from .limits import ConcurrencyLimiter


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


def _render(
    url: str,
    final_url: str,
    title: str,
    status_code: int | None,
    markdown: str,
    query: str | None,
    cfg: Config,
    *,
    cache_state: str,
    age_s: int | None = None,
    degraded_from: str | None = None,
) -> dict:
    """把 raw markdown 組裝成回傳封包。

    快取命中與新抓取共用這條路徑 —— 選節永遠是從 raw 重算的,所以同一頁換 query
    重查不需重爬,這正是存 raw 而非 fit 的理由。
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
            f"重新抓取失敗{f' ({degraded_from})' if degraded_from else ''},"
            f"回傳 {age_s} 秒前的快取內容。內容可能已過時。"
        )

    # 小頁面直接回全文。省下的 token 不值得冒選錯的風險。
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
        # 沒有任何標題可切(少見)。退回硬截斷。
        text = tokens.truncate(markdown, cfg.select_budget_tokens)
        env.update({
            "mode": "truncate",
            "returned_tokens": tokens.count(text),
            "truncated": True,
            "content": text,
            "note": env.get("note") or "頁面沒有標題結構,已按文件順序截斷。",
        })
        return env

    sel = ranking.select(secs, query, cfg.select_budget_tokens, cfg.max_section_frac)
    picked = {p.section.id for p in sel.picks}

    env.update({
        "mode": sel.strategy,
        "query_matched": sel.matched,
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
        # 未納入的章節與各自成本 —— agent 據此判斷答案是否在別處、取回划不划算。
        "outline_omitted": [
            s.as_outline_entry() for s in secs if s.id not in picked
        ][:40],
    })
    if not sel.matched and query and not env.get("note"):
        env["note"] = "查詢在此頁沒有任何匹配,已改用文件順序截斷。可能取錯頁面了。"
    return env


async def fetch(
    url: str,
    query: str | None,
    cfg: Config,
    client: CrawlClient,
    cache: CacheStore | None = None,
    limiter: ConcurrencyLimiter | None = None,
) -> dict:
    verdict = admission.check(url)
    if not verdict.allowed:
        return _error_envelope(url, verdict.reason, verdict.detail)

    cached = await cache.get(url) if cache else None
    if cached is not None and cached.is_fresh(cache.max_age_for(url)):
        return _render(
            url, cached.final_url, cached.title, cached.status_code, cached.markdown,
            query, cfg, cache_state="hit", age_s=cached.age_s,
        )

    # 併發上限:超過時排隊等待而非拒絕 —— 抓取本來就慢,多等優於直接失敗。
    if limiter is not None:
        async with limiter:
            crawled = await client.fetch(url)
    else:
        crawled = await client.fetch(url)
    if not crawled.ok:
        # 抓取失敗但手上還有保留期內的舊資料 —— 回舊的並標記 stale。
        # 反爬阻擋在實測中很常見,此時一份舊內容遠勝於一個錯誤碼。
        if cached is not None:
            return _render(
                url, cached.final_url, cached.title, cached.status_code, cached.markdown,
                query, cfg, cache_state="stale", age_s=cached.age_s,
                degraded_from=oc.FETCH_FAILED,
            )
        code = oc.TIMEOUT if crawled.transport_error == "timeout" else oc.FETCH_FAILED
        return _error_envelope(url, code, crawled.transport_error)

    result = crawled.result
    markdown = CrawlClient.raw_markdown(result)

    final_url = result.get("redirected_url") or url
    redirect_verdict = admission.check_redirect(url, final_url)
    if not redirect_verdict.allowed:
        # 落點不合規時丟棄內容,而且不回舊快取 —— 這是安全事件,不是暫時性失敗。
        return _error_envelope(url, redirect_verdict.reason, redirect_verdict.detail)

    status = oc.classify(result, markdown)
    if not status.ok:
        if cached is not None:
            return _render(
                url, cached.final_url, cached.title, cached.status_code, cached.markdown,
                query, cfg, cache_state="stale", age_s=cached.age_s,
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

    return _render(
        url, final_url, title, result.get("status_code"), markdown,
        query, cfg, cache_state="miss",
    )
