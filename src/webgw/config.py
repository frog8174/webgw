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

    # 選節預算。實測 (12 查詢 / 3 頁面):4000 tok 命中 12/12,2000 tok 命中 11/12。
    select_budget_tokens: int = _int("SELECT_BUDGET_TOKENS", 4000)
    # 小於此值的頁面直接回傳全文,不做選節 —— 省下的 token 不值得冒砍錯的風險。
    passthrough_max_tokens: int = _int("PASSTHROUGH_MAX_TOKENS", 4000)
    # 單一章節最多佔預算的比例。防止超大章節(如 OpenReview 的 21k tok 單節)炸掉預算。
    max_section_frac: float = _float("MAX_SECTION_FRAC", 0.5)

    fetch_timeout_s: float = _float("FETCH_TIMEOUT_S", 30.0)

    # DNS rebinding 防護的允許 Host 清單 (逗號分隔)。
    # 走 NodePort 時務必加入 <節點IP>:<port>,否則請求會被回 421。
    # 設為 "*" 停用防護 —— 僅建議在完全信任的區網。
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
