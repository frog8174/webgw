"""raw markdown 快取。

存 raw 而非 fit:換 query 重查同一頁不需重爬,且 fit_markdown 實測不可信
(PruningContentFilter 會砍掉文章標題卻留下登入元件)。

三層時間語意 —— 不要混為一談:
  max_age    (新鮮度) 超過就重抓。逐網域可調。
  retention  (保留期) 超過就刪除,資料不再可用。預設 14 天。
  stale      介於兩者之間、且重抓失敗時,回舊資料並標記 —— 實測反爬阻擋很常見
             (Reuters/Medium),此時一份三天前的內容遠勝於一個錯誤碼。

容量上限與時間清理必須並存。只有時間限制擋不住暴衝的爬取量 —— 上游 crawl4ai
自己的 SQLite 就是「無 TTL、無容量上限,長到磁碟滿」的反例。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("webgw.cache")

# 追蹤參數:不影響頁面內容,但會讓同一頁存成多份,拖垮命中率。
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "matomo_")
_TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "ref", "referrer", "source", "spm", "share_source", "yclid",
    "_ga", "_gl", "wt_mc", "trk",
}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """正規化以提高命中率。保守處理:只動確定無語意的部分。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    netloc = host
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = host + ":" + str(parts.port)

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    # 排序讓參數順序不同的同一頁能命中同一筆。
    query = urlencode(sorted(query_pairs), doseq=True)

    path = parts.path or "/"
    # 刻意不去掉尾斜線:部分站台 /a 與 /a/ 是不同頁面。
    # fragment 一律丟棄 —— 它不會送到伺服器,對內容沒有影響。
    return urlunsplit((scheme, netloc, path, query, ""))


def cache_key(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:32]


@dataclass
class CacheEntry:
    url: str
    final_url: str
    title: str
    markdown: str
    raw_tokens: int
    status_code: int | None
    fetched_at: int
    hits: int

    @property
    def age_s(self) -> int:
        return max(0, int(time.time()) - self.fetched_at)

    def is_fresh(self, max_age_s: int) -> bool:
        return self.age_s < max_age_s


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    key          TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    final_url    TEXT,
    title        TEXT,
    markdown     TEXT NOT NULL,
    raw_tokens   INTEGER NOT NULL,
    status_code  INTEGER,
    fetched_at   INTEGER NOT NULL,
    -- REAL 而非 INTEGER:整秒精度會讓同一秒內的存取全部同分,
    -- ORDER BY 退化成任意順序,LRU 形同失效。
    last_hit_at  REAL NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0,
    nbytes       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_fetched_at  ON pages(fetched_at);
CREATE INDEX IF NOT EXISTS idx_pages_last_hit_at ON pages(last_hit_at);
"""


class CacheStore:
    def __init__(
        self,
        path: str,
        *,
        retention_days: int = 14,
        default_max_age_s: int = 86_400,
        domain_rules: dict[str, int] | None = None,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.path = path
        self.retention_s = retention_days * 86_400
        self.default_max_age_s = default_max_age_s
        self.domain_rules = domain_rules or {}
        self.max_bytes = max_bytes
        self._janitor: asyncio.Task | None = None
        self._init_db()

    # ── 連線 ────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            # auto_vacuum 必須在建表**之前**設定,否則對既有資料庫不生效。
            # 沒有它,DELETE 不會把空間還給作業系統,檔案只會單向長大。
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ── 新鮮度 ──────────────────────────────────────────────────────
    def max_age_for(self, url: str) -> int:
        """逐網域新鮮度,並夾在保留期以內。

        max_age 超過保留期是無意義的設定 —— 資料會先被刪掉,永遠等不到過期。
        """
        host = (urlsplit(url).hostname or "").lower()
        max_age = self.default_max_age_s
        # 由最長後綴開始比對,讓 docs.example.com 的規則優先於 example.com。
        for domain, secs in sorted(self.domain_rules.items(), key=lambda kv: -len(kv[0])):
            if host == domain or host.endswith("." + domain):
                max_age = secs
                break
        return min(max_age, self.retention_s)

    # ── 讀寫 ────────────────────────────────────────────────────────
    def _get_sync(self, key: str) -> CacheEntry | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT url, final_url, title, markdown, raw_tokens, status_code,"
                " fetched_at, hits FROM pages WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            now = int(time.time())
            # 超過保留期的資料視同不存在(實際刪除留給下一輪清理)。
            if now - row[6] > self.retention_s:
                return None
            conn.execute(
                "UPDATE pages SET hits=hits+1, last_hit_at=? WHERE key=?",
                (time.time(), key),
            )
            conn.commit()
            return CacheEntry(
                url=row[0], final_url=row[1] or row[0], title=row[2] or "",
                markdown=row[3], raw_tokens=row[4], status_code=row[5],
                fetched_at=row[6], hits=row[7],
            )
        finally:
            conn.close()

    async def get(self, url: str) -> CacheEntry | None:
        return await asyncio.to_thread(self._get_sync, cache_key(url))

    def _put_sync(self, key: str, url: str, entry: dict) -> None:
        now = int(time.time())
        md = entry["markdown"]
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO pages (key,url,final_url,title,markdown,raw_tokens,"
                " status_code,fetched_at,last_hit_at,hits,nbytes)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "  final_url=excluded.final_url, title=excluded.title,"
                "  markdown=excluded.markdown, raw_tokens=excluded.raw_tokens,"
                "  status_code=excluded.status_code, fetched_at=excluded.fetched_at,"
                "  last_hit_at=excluded.last_hit_at, nbytes=excluded.nbytes",
                (key, url, entry.get("final_url"), entry.get("title"), md,
                 entry.get("raw_tokens", 0), entry.get("status_code"), now, time.time(),
                 len(md.encode("utf-8"))),
            )
            conn.commit()
        finally:
            conn.close()

    async def put(self, url: str, **entry) -> None:
        await asyncio.to_thread(self._put_sync, cache_key(url), normalize_url(url), entry)

    # ── 清理 ────────────────────────────────────────────────────────
    def _cleanup_sync(self) -> dict:
        now = int(time.time())
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM pages WHERE fetched_at < ?", (now - self.retention_s,)
            )
            expired = cur.rowcount

            # 容量上限:依 last_hit_at 由舊到新淘汰 (LRU),直到低於上限。
            total = conn.execute("SELECT COALESCE(SUM(nbytes),0) FROM pages").fetchone()[0]
            evicted = 0
            if total > self.max_bytes:
                rows = conn.execute(
                    "SELECT key, nbytes FROM pages ORDER BY last_hit_at ASC"
                ).fetchall()
                for key, nbytes in rows:
                    if total <= self.max_bytes:
                        break
                    conn.execute("DELETE FROM pages WHERE key=?", (key,))
                    total -= nbytes
                    evicted += 1
            conn.commit()

            # DELETE 之後要回收空間,否則檔案只會單向長大。
            if expired or evicted:
                conn.execute("PRAGMA incremental_vacuum")
                conn.commit()

            remaining = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            return {
                "expired": expired, "evicted": evicted,
                "remaining": remaining, "bytes": total,
            }
        finally:
            conn.close()

    async def cleanup(self) -> dict:
        stats = await asyncio.to_thread(self._cleanup_sync)
        log.info(
            "cache cleanup: 過期刪除=%d LRU淘汰=%d 剩餘=%d 佔用=%.1fMB",
            stats["expired"], stats["evicted"], stats["remaining"],
            stats["bytes"] / 1024 / 1024,
        )
        return stats

    def start_janitor(self, interval_s: int) -> None:
        """啟動週期清理。重複呼叫不會重複啟動。"""
        if self._janitor is not None and not self._janitor.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await self.cleanup()
                except Exception:  # noqa: BLE001
                    log.exception("cache cleanup 失敗,下一輪重試")
                await asyncio.sleep(interval_s)

        try:
            self._janitor = asyncio.get_running_loop().create_task(_loop())
            log.info(
                "cache janitor 已啟動,間隔 %ds,保留期 %d 天",
                interval_s, self.retention_s // 86_400,
            )
        except RuntimeError:
            log.warning("沒有執行中的 event loop,janitor 未啟動")
