"""環境變數設定。所有可調參數集中於此,不散落在程式各處。"""
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
    # 上游 crawl4ai。本機測試指向 127.0.0.1,上線後指向區網位址。
    # 注意:這個位址是 gateway 的上游,不受 admission 的私網封鎖規則約束。
    crawl4ai_base_url: str = os.environ.get("CRAWL4AI_BASE_URL", "http://127.0.0.1:11235")
    crawl4ai_token: str = os.environ.get("CRAWL4AI_TOKEN", "")

    host: str = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port: int = _int("GATEWAY_PORT", 8080)

    # 選節預算。實測台積電那頁 (92k tokens):4000 ✗、6000 ✗、8000 ✓。
    # 不用百分比 —— 地端模型 context 只有 8k~32k,百分比遇到超大頁面會爆掉。
    select_budget_tokens: int = _int("SELECT_BUDGET_TOKENS", 8000)
    # 小於此值的頁面直接回傳全文,不做選節 —— 省下的 token 不值得冒砍錯的風險。
    passthrough_max_tokens: int = _int("PASSTHROUGH_MAX_TOKENS", 4000)
    # 單一章節最多佔預算的比例。0.5 時實測有一次只裝進 1 個段落就用完預算,
    # 降到 0.35 能塞進至少 3 個不同段落,覆蓋面明顯較好。
    max_section_frac: float = _float("MAX_SECTION_FRAC", 0.35)

    fetch_timeout_s: float = _float("FETCH_TIMEOUT_S", 30.0)

    # DNS rebinding 防護的允許 Host 清單 (逗號分隔)。
    # 走 NodePort 時務必加入 <節點IP>:<port>,否則請求會被回 421。
    # 設為 "*" 停用防護 —— 僅建議在完全信任的區網。
    # MCP stateless 模式。stateless 時伺服器不發 Mcp-Session-Id,
    # 客戶端開的 GET /mcp SSE 串流沒有 session 可依附 —— 實測 OpenCode 會
    # 因此取不到工具清單。預設關閉:反正快取是 SQLite 單檔、replicas 鎖在 1,
    # stateless 目前買不到水平擴展。要擴展時再連同快取後端一起改。
    # ── 檢索 ────────────────────────────────────────────────────────
    # bm25 (預設,約 2.5 秒) | rerank (約 5~8 秒)
    # 實測 30 個 ground-truth 案例:bm25 rank@1 24/30,rerank 28/30。
    # 沒有 auto 模式 —— 實驗證明沒有訊號能預測 BM25 何時會錯,見 retrieval.py。
    retrieval_mode: str = os.environ.get("RETRIEVAL_MODE", "bm25")
    reranker_url: str = os.environ.get("RERANKER_URL", "")
    reranker_model: str = os.environ.get("RERANKER_MODEL", "bge-reranker-v2-m3")
    rerank_top_n: int = _int("RERANK_TOP_N", 30)
    rerank_timeout_s: float = _float("RERANK_TIMEOUT_S", 15.0)
    # 單段送出的字元上限。cross-encoder 把 query 和 document 接在一起送,
    # 超過模型 max_model_len 的部分會被截掉,可能截掉答案所在處。
    rerank_doc_chars: int = _int("RERANK_DOC_CHARS", 2000)

    # ── 限流 ────────────────────────────────────────────────────────
    # 上游 crawl4ai 自己沒有任何限流 (啟動日誌:work queue per_principal=unlimited),
    # 而每次抓取都會開一個真實瀏覽器分頁,是重負載。
    max_concurrent_fetches: int = _int("MAX_CONCURRENT_FETCHES", 4)
    # 每個來源每分鐘的請求上限。0 = 不限。
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 60)

    # ── 認證 ────────────────────────────────────────────────────────
    # 空字串 = 不啟用(僅適合單機 loopback 開發)。
    # MCP 規格的傳輸安全章節:伺服器 SHOULD 對所有連線實作認證。
    auth_token: str = os.environ.get("WEBGW_AUTH_TOKEN", "")

    # MCP 回應格式。False = SSE 分幀 (event-stream),True = 純 JSON。
    # 實測 SSE 模式下 OpenCode 的連線是間歇性的 (6 次中 3 次取不到工具清單):
    # 它開了 GET /mcp 的 SSE 串流之後就不送 tools/list。
    mcp_json_response: bool = os.environ.get("MCP_JSON_RESPONSE", "1") in ("1", "true", "True")

    mcp_stateless: bool = os.environ.get("MCP_STATELESS", "0") in ("1", "true", "True")

    # ── 快取 ────────────────────────────────────────────────────────
    # 存 raw markdown。存 raw 而非 fit 的理由:換 query 重查同一頁不必重爬,
    # 而 fit_markdown 實測不可信(會砍掉文章標題留下登入元件)。
    cache_path: str = os.environ.get("CACHE_PATH", "/data/cache.sqlite3")
    cache_enabled: bool = os.environ.get("CACHE_ENABLED", "1") not in ("0", "false", "False")

    # 保留期:超過就刪除,資料不再可用。
    cache_retention_days: int = _int("CACHE_RETENTION_DAYS", 14)
    # 新鮮度:超過就重抓,但資料還在(重抓失敗時可回 stale)。
    # 必然 <= 保留期,否則永遠等不到過期就被刪了 —— 由 effective_max_age 夾住。
    cache_default_max_age_s: int = _int("CACHE_DEFAULT_MAX_AGE_S", 86_400)
    # 逐網域覆寫,格式:"technews.tw=3600,docs.crawl4ai.com=604800"
    cache_domain_rules_raw: str = os.environ.get("CACHE_DOMAIN_RULES", "")

    # 容量上限。只靠時間清理擋不住暴衝的爬取量 —— 上游 crawl4ai 自己的 SQLite
    # 就是「無 TTL、無容量上限,會長到磁碟滿」,不要重蹈覆轍。
    cache_max_bytes: int = _int("CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024)
    # 清理排程間隔。
    cache_cleanup_interval_s: int = _int("CACHE_CLEANUP_INTERVAL_S", 3_600)

    mcp_allowed_hosts: tuple[str, ...] = tuple(
        h.strip()
        for h in os.environ.get("MCP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
        if h.strip()
    )


CONFIG = Config()


def expand_allowed_hosts(hosts: tuple[str, ...]) -> list[str]:
    """把不含埠號的主機展開成同時允許任意埠。

    上游的 Host 比對是完全相符**含埠號**的 —— 設定 "127.0.0.1" 而請求帶
    Host: 127.0.0.1:8080 會被擋成 421。NodePort 部署時 Host 是 <節點IP>:<nodePort>,
    這個坑幾乎必踩,所以在此自動補上 "host:*" 萬用埠形式。
    """
    out: list[str] = []
    for h in hosts:
        out.append(h)
        if ":" not in h:
            out.append(f"{h}:*")
    return out


def parse_domain_rules(raw: str) -> dict[str, int]:
    """解析 "domain=seconds,domain=seconds" 形式的逐網域新鮮度設定。"""
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
    """決定實際綁定位址,並回傳需要提醒的訊息。

    沒有設定 token 卻要綁 0.0.0.0,等於把服務對整個網路開放且不設防 ——
    這正是實測中發生過的組合。此時強制降級為只綁 127.0.0.1。

    做法照抄上游 crawl4ai 0.9.2:它在沒有 API token 時拒絕綁 0.0.0.0,
    只綁容器內 loopback。那個設計當時擋住了一個真實的暴露風險。
    """
    if cfg.auth_token:
        return cfg.host, None
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1", (
            f"未設定 WEBGW_AUTH_TOKEN,拒絕綁定 {cfg.host},已降級為 127.0.0.1。"
            "要對外提供服務請先設定 token。"
        )
    return cfg.host, "未設定 WEBGW_AUTH_TOKEN —— 所有端點皆無認證(僅適合本機開發)。"
