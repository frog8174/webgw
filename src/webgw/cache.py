"""Raw markdown cache.

Stores raw rather than fit markdown: re-querying the same page with a different
query then needs no re-crawl, and fit_markdown was measured untrustworthy
(PruningContentFilter drops article headings while keeping login widgets).

Three time semantics, which must not be conflated:
    max_age    freshness -- past this the page is re-fetched. Per-domain.
    retention  past this the row is deleted and the data is gone. 14 days.
    stale      between the two, when a re-fetch fails, the old copy is returned
               and flagged. Anti-bot blocking is common (Reuters, Medium), and
               three-day-old content beats an error code by a wide margin.

The size ceiling and the time-based cleanup must coexist. Time limits alone do
not hold back a burst of crawling -- upstream crawl4ai's own SQLite is the
counterexample: no TTL, no size cap, grows until the disk fills.
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

# Tracking parameters: they do not change page content, but they do store the
# same page many times over and wreck the hit rate.
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "matomo_")
_TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "ref", "referrer", "source", "spm", "share_source", "yclid",
    "_ga", "_gl", "wt_mc", "trk",
}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Normalize to raise the hit rate.

    Deliberately conservative: only parts with no possible semantic meaning are
    touched.
    """
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
    # Sorting lets the same page hit one row regardless of parameter order.
    query = urlencode(sorted(query_pairs), doseq=True)

    path = parts.path or "/"
    # Trailing slashes are deliberately kept: on some sites /a and /a/ are
    # different pages. The fragment is always dropped -- it never reaches the
    # server and cannot affect content.
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
    -- REAL, not INTEGER: whole-second precision ties every access within the
    -- same second, ORDER BY degenerates into arbitrary order, and LRU stops
    -- working entirely.
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

    # -- connection --------------------------------------------------------
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
            # auto_vacuum must be set *before* the tables are created; it has no
            # effect on an existing database. Without it, DELETE never returns
            # space to the OS and the file only ever grows.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # -- freshness ---------------------------------------------------------
    def max_age_for(self, url: str) -> int:
        """Per-domain freshness, clamped to the retention period.

        A max_age beyond retention is a meaningless setting -- the row is
        deleted first, so it never gets the chance to go stale.
        """
        host = (urlsplit(url).hostname or "").lower()
        max_age = self.default_max_age_s
        # Match longest suffix first so a rule for docs.example.com wins over
        # one for example.com.
        for domain, secs in sorted(self.domain_rules.items(), key=lambda kv: -len(kv[0])):
            if host == domain or host.endswith("." + domain):
                max_age = secs
                break
        return min(max_age, self.retention_s)

    # -- read and write ----------------------------------------------------
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
            # Rows past retention are treated as absent; the actual delete is
            # left to the next cleanup pass.
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

    # -- cleanup -----------------------------------------------------------
    def _cleanup_sync(self) -> dict:
        now = int(time.time())
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM pages WHERE fetched_at < ?", (now - self.retention_s,)
            )
            expired = cur.rowcount

            # Size ceiling: evict least recently used first, by last_hit_at,
            # until back under the limit.
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

            # Space has to be reclaimed after DELETE, or the file only grows.
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
            "cache cleanup: expired=%d evicted=%d remaining=%d size=%.1fMB",
            stats["expired"], stats["evicted"], stats["remaining"],
            stats["bytes"] / 1024 / 1024,
        )
        return stats

    def start_janitor(self, interval_s: int) -> None:
        """Start periodic cleanup. Calling this again is a no-op."""
        if self._janitor is not None and not self._janitor.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await self.cleanup()
                except Exception:  # noqa: BLE001
                    log.exception("cache cleanup failed, retrying next cycle")
                await asyncio.sleep(interval_s)

        try:
            self._janitor = asyncio.get_running_loop().create_task(_loop())
            log.info(
                "cache janitor started, interval %ds, retention %d days",
                interval_s, self.retention_s // 86_400,
            )
        except RuntimeError:
            log.warning("no running event loop; janitor not started")
