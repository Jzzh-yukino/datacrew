"""进程内有界 LRU，缓存 SQL 文本并使用固定 TTL。"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class _Entry:
    sql: str
    expires_at: float
    size: int


class SqlCache:
    def __init__(
        self,
        *,
        max_entries: int = 1_000,
        max_bytes: int = 8 * 1024 * 1024,
        max_entry_bytes: int = 16 * 1024,
        ttl_s: float = 1800,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._size = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_entry_bytes = max_entry_bytes
        self._ttl_s = ttl_s
        self._clock = clock

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            self.discard(key)
            return None
        self._entries.move_to_end(key)
        return entry.sql

    def discard(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._size -= entry.size

    def prune_expired(self) -> None:
        now = self._clock()
        for key in [key for key, entry in self._entries.items() if entry.expires_at <= now]:
            self.discard(key)

    def put(self, key: str, sql: str) -> None:
        self.prune_expired()
        self.discard(key)
        # UTF-8 载荷包含 key、SQL 和 8 字节的到期时间。
        size = len(key.encode()) + len(sql.encode()) + 8
        if self._max_entries <= 0 or size > min(self._max_entry_bytes, self._max_bytes):
            return
        while self._size + size > self._max_bytes or len(self._entries) >= self._max_entries:
            _, evicted = self._entries.popitem(last=False)
            self._size -= evicted.size
        self._entries[key] = _Entry(sql, self._clock() + self._ttl_s, size)
        self._size += size


sql_cache = SqlCache()
