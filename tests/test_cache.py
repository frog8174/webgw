"""Cache layer: URL normalization, freshness, retention cleanup, LRU eviction."""
from __future__ import annotations

import time

import pytest

from webgw.cache import CacheStore, cache_key, normalize_url

DAY = 86_400


@pytest.fixture
def store(tmp_path):
    return CacheStore(
        str(tmp_path / "c.sqlite3"),
        retention_days=14,
        default_max_age_s=DAY,
        domain_rules={"technews.tw": 3600, "docs.example.com": 7 * DAY},
        max_bytes=10_000,
    )


# -- URL normalization -------------------------------------------------------
def test_tracking_params_stripped():
    a = normalize_url("https://Example.com/a?utm_source=x&id=7&fbclid=zz")
    assert a == "https://example.com/a?id=7"


def test_param_order_and_fragment_do_not_split_cache():
    assert cache_key("https://e.com/a?b=1&a=2#top") == cache_key("https://e.com/a?a=2&b=1")


def test_default_port_normalised():
    assert normalize_url("https://e.com:443/a") == normalize_url("https://e.com/a")


def test_trailing_slash_is_significant():
    """On some sites /a and /a/ are different pages, so they are deliberately
    not merged."""
    assert cache_key("https://e.com/a") != cache_key("https://e.com/a/")


# -- freshness ---------------------------------------------------------------
def test_domain_rule_overrides_default(store):
    assert store.max_age_for("https://technews.tw/2026/08/x") == 3600
    assert store.max_age_for("https://other.com/x") == DAY


def test_subdomain_matches_longest_rule_first(store):
    assert store.max_age_for("https://docs.example.com/x") == 7 * DAY


def test_max_age_capped_by_retention(tmp_path):
    """Freshness beyond retention is a meaningless setting -- the row is deleted
    first, so it never gets the chance to go stale."""
    s = CacheStore(
        str(tmp_path / "c.sqlite3"),
        retention_days=14,
        domain_rules={"docs.site": 30 * DAY},   # 30 days > the 14-day retention
    )
    assert s.max_age_for("https://docs.site/x") == 14 * DAY


# -- read and write ----------------------------------------------------------
@pytest.mark.asyncio
async def test_put_then_get_roundtrip(store):
    await store.put("https://e.com/a", markdown="# hi", raw_tokens=3, status_code=200,
                    final_url="https://e.com/a", title="T")
    got = await store.get("https://e.com/a?utm_source=news")   # same row after normalization
    assert got is not None and got.markdown == "# hi" and got.title == "T"


@pytest.mark.asyncio
async def test_miss_returns_none(store):
    assert await store.get("https://never.seen/x") is None


def test_freshness_boundary(store):
    from webgw.cache import CacheEntry

    now = int(time.time())
    fresh = CacheEntry("u", "u", "", "md", 1, 200, now - 100, 0)
    stale = CacheEntry("u", "u", "", "md", 1, 200, now - 2 * DAY, 0)
    assert fresh.is_fresh(DAY) is True
    assert stale.is_fresh(DAY) is False


# -- cleanup -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_retention_deletes_old_rows(store):
    await store.put("https://e.com/old", markdown="x" * 100, raw_tokens=1)
    # Push fetched_at back 15 days, past the 14-day retention.
    conn = store._connect()
    conn.execute("UPDATE pages SET fetched_at=?", (int(time.time()) - 15 * DAY,))
    conn.commit()
    conn.close()

    assert await store.get("https://e.com/old") is None      # immediately treated as absent
    stats = await store.cleanup()
    assert stats["expired"] == 1 and stats["remaining"] == 0


@pytest.mark.asyncio
async def test_retention_keeps_recent_rows(store):
    await store.put("https://e.com/new", markdown="x" * 100, raw_tokens=1)
    stats = await store.cleanup()
    assert stats["expired"] == 0 and stats["remaining"] == 1


@pytest.mark.asyncio
async def test_lru_eviction_on_size_cap(store):
    """Time-based cleanup alone does not hold back a burst of crawling, so the
    size ceiling has to work alongside it."""
    for i in range(6):
        await store.put(f"https://e.com/{i}", markdown="y" * 3000, raw_tokens=1)
    # Touch row 0 so it becomes most recently used; it should survive.
    await store.get("https://e.com/0")

    stats = await store.cleanup()
    assert stats["bytes"] <= store.max_bytes
    assert stats["evicted"] > 0
    assert await store.get("https://e.com/0") is not None
