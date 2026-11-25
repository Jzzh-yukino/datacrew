from app.infra.sql_cache import SqlCache


def test_repeated_writes_of_same_key_do_not_evict_another_key() -> None:
    cache = SqlCache(max_entries=2, max_bytes=48)
    cache.put("other", "SELECT 10")
    cache.put("same", "SELECT 20")

    for _ in range(100):
        cache.put("same", "SELECT 300")

    assert (cache.get("other"), cache.get("same")) == ("SELECT 10", "SELECT 300")


def test_cache_hit_updates_lru_order_without_extending_expiration() -> None:
    now = 0
    cache = SqlCache(max_entries=2, ttl_s=10, clock=lambda: now)
    cache.put("read-again", "SELECT 1")
    cache.put("unread", "SELECT 2")

    now = 5
    assert cache.get("read-again") == "SELECT 1"
    cache.put("new", "SELECT 3")
    assert cache.get("unread") is None

    now = 10
    assert (cache.get("read-again"), cache.get("new")) == (None, "SELECT 3")
