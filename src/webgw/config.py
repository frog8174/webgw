"""Environment configuration. Every tunable lives here rather than scattered
through the code."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # Upstream crawl4ai. Points at 127.0.0.1 for local testing, at a LAN or
    # cluster address in production. Note: this is the gateway's own upstream
    # and is deliberately not subject to admission's private-network rules.
    crawl4ai_base_url: str = os.environ.get("CRAWL4AI_BASE_URL", "http://127.0.0.1:11235")
    crawl4ai_token: str = os.environ.get("CRAWL4AI_TOKEN", "")

    host: str = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port: int = _int("GATEWAY_PORT", 8080)

    # Selection budget. Measured on a 92k-token page: 4000 failed, 6000 failed,
    # 8000 succeeded. Deliberately not a percentage -- local models have only
    # 8k-32k of context, and a percentage blows up on very large pages.
    select_budget_tokens: int = _int("SELECT_BUDGET_TOKENS", 8000)
    # Pages under this size are returned whole with no selection -- the tokens
    # saved are not worth the risk of cutting the wrong part.
    passthrough_max_tokens: int = _int("PASSTHROUGH_MAX_TOKENS", 4000)
    # Largest share of the budget one section may take. At 0.5 a single section
    # was measured consuming the budget on its own, fitting just 1 passage;
    # 0.35 fits at least 3 distinct passages, with clearly better coverage.
    max_section_frac: float = _float("MAX_SECTION_FRAC", 0.35)

    fetch_timeout_s: float = _float("FETCH_TIMEOUT_S", 30.0)

    # -- Retrieval ---------------------------------------------------------
    # bm25 (default, about 2.5s) | rerank (about 5-8s).
    # Measured on 30 ground-truth cases: bm25 rank@1 24/30, rerank 28/30.
    # There is no auto mode -- no signal predicts when BM25 is wrong; see
    # retrieval.py for the experiment.
    retrieval_mode: str = os.environ.get("RETRIEVAL_MODE", "bm25")
    reranker_url: str = os.environ.get("RERANKER_URL", "")
    reranker_model: str = os.environ.get("RERANKER_MODEL", "bge-reranker-v2-m3")
    rerank_top_n: int = _int("RERANK_TOP_N", 30)
    rerank_timeout_s: float = _float("RERANK_TIMEOUT_S", 15.0)
    # Character cap per passage. A cross-encoder concatenates query and document
    # before encoding, so anything past the model's max_model_len is cut --
    # possibly cutting off where the answer sits.
    rerank_doc_chars: int = _int("RERANK_DOC_CHARS", 2000)
    # API key for a commercial reranking service (Cohere, Jina, Voyage, ...).
    # Self-hosted vLLM does not need one. When set it is sent as
    # `Authorization: Bearer` -- these services share the request and response
    # shape of vLLM's /v1/rerank, and differ only in authentication.
    reranker_api_key: str = os.environ.get("RERANKER_API_KEY", "")

    # -- Limits ------------------------------------------------------------
    # Upstream crawl4ai applies no throttling of its own (startup log:
    # "work queue per_principal=unlimited"), and every fetch opens a real
    # browser tab, which is a heavy load.
    max_concurrent_fetches: int = _int("MAX_CONCURRENT_FETCHES", 4)
    # Requests per minute per caller. 0 disables the limit.
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 60)

    # -- Authentication ----------------------------------------------------
    # Empty string disables it, which suits single-machine loopback development
    # only. The MCP transport security section says servers SHOULD authenticate
    # all connections.
    auth_token: str = os.environ.get("WEBGW_AUTH_TOKEN", "")

    # MCP response format. False = SSE framing (event-stream), True = plain
    # JSON. Under SSE, OpenCode's connection was measured as intermittent
    # (3 of 6 attempts got no tool list): it opens the GET /mcp SSE stream and
    # then never sends tools/list.
    mcp_json_response: bool = os.environ.get("MCP_JSON_RESPONSE", "1") in ("1", "true", "True")

    # MCP stateless mode. When stateless the server issues no Mcp-Session-Id, so
    # the client's GET /mcp SSE stream has no session to attach to -- measured
    # leaving OpenCode unable to retrieve the tool list. Off by default: the
    # cache is a single SQLite file with replicas pinned to 1, so stateless buys
    # no horizontal scaling today. Revisit together with the cache backend.
    mcp_stateless: bool = os.environ.get("MCP_STATELESS", "0") in ("1", "true", "True")

    # -- Cache -------------------------------------------------------------
    # Stores raw markdown. Raw rather than fit, for two reasons: re-querying the
    # same page with a different query needs no re-crawl, and fit_markdown was
    # measured untrustworthy (it drops article headings while keeping login
    # widgets).
    cache_path: str = os.environ.get("CACHE_PATH", "/data/cache.sqlite3")
    cache_enabled: bool = os.environ.get("CACHE_ENABLED", "1") not in ("0", "false", "False")

    # Retention: past this the row is deleted and the data is gone.
    cache_retention_days: int = _int("CACHE_RETENTION_DAYS", 14)
    # Freshness: past this the page is re-fetched, but the row survives, so a
    # failed re-fetch can still serve stale content. Necessarily <= retention,
    # otherwise a row is deleted before it ever goes stale -- clamped by
    # effective_max_age.
    cache_default_max_age_s: int = _int("CACHE_DEFAULT_MAX_AGE_S", 86_400)
    # Per-domain overrides, e.g. "technews.tw=3600,docs.crawl4ai.com=604800".
    cache_domain_rules_raw: str = os.environ.get("CACHE_DOMAIN_RULES", "")

    # Size ceiling. Time-based cleanup alone does not hold back a burst of
    # crawling -- upstream crawl4ai's own SQLite has no TTL and no size cap and
    # grows until the disk fills. Do not repeat that.
    cache_max_bytes: int = _int("CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024)
    # Cleanup schedule interval.
    cache_cleanup_interval_s: int = _int("CACHE_CLEANUP_INTERVAL_S", 3_600)

    # Allowed Host values for DNS rebinding protection (comma separated).
    # On a NodePort deployment this must include <node IP>:<port>, or requests
    # come back 421. Set to "*" to disable the protection -- only advisable on a
    # fully trusted network.
    mcp_allowed_hosts: tuple[str, ...] = tuple(
        h.strip()
        for h in os.environ.get("MCP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
        if h.strip()
    )


CONFIG = Config()


def expand_allowed_hosts(hosts: tuple[str, ...]) -> list[str]:
    """Let hosts given without a port also match any port.

    The upstream Host check is an exact match *including the port*, so
    configuring "127.0.0.1" while the request carries `Host: 127.0.0.1:8080`
    is rejected with 421. On a NodePort deployment the Host is
    <node IP>:<nodePort>, which makes this near-certain to bite, so a "host:*"
    wildcard form is added automatically.
    """
    out: list[str] = []
    for h in hosts:
        out.append(h)
        if ":" not in h:
            out.append(f"{h}:*")
    return out


def parse_domain_rules(raw: str) -> dict[str, int]:
    """Parse per-domain freshness rules of the form "domain=seconds,domain=seconds"."""
    rules: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        domain, _, secs = part.partition("=")
        try:
            rules[domain.strip().lower()] = int(secs)
        except ValueError:
            continue
    return rules


def effective_host(cfg: "Config") -> tuple[str, str | None]:
    """Decide the address to bind, and return any warning worth surfacing.

    Binding 0.0.0.0 with no token set exposes the service to the whole network
    with no protection -- a combination that did occur during testing. In that
    case the binding is forced down to 127.0.0.1.

    The approach is taken from upstream crawl4ai 0.9.2, which refuses to bind
    0.0.0.0 without an API token and stays on the container's loopback. That
    design caught a real exposure at the time.
    """
    if cfg.auth_token:
        return cfg.host, None
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1", (
            f"WEBGW_AUTH_TOKEN is not set; refusing to bind {cfg.host}, "
            "downgraded to 127.0.0.1. Set a token before serving externally."
        )
    return cfg.host, (
        "WEBGW_AUTH_TOKEN is not set -- every endpoint is unauthenticated "
        "(suitable for local development only)."
    )
