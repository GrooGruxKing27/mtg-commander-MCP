import re
import time
from typing import Any


def to_slug(name: str) -> str:
    """Convert a card/commander name to a URL slug.

    Examples:
        "Atraxa, Praetors' Voice" -> "atraxa-praetors-voice"
        "Sol Ring" -> "sol-ring"
        "Expansion // Explosion" -> "expansion-explosion"
    """
    slug = name.lower()
    slug = re.sub(r"['\",:.!?&]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


class Cache:
    """In-memory TTL + LRU cache.

    TTL evicts entries by age, ``max_size`` caps total entries (oldest-first
    eviction). Both bounds are needed for a long-running daemon: TTL alone
    lets the cache grow without limit if every entry is accessed within its
    window, and LRU alone lets stale data linger indefinitely.
    """

    def __init__(self, ttl: float = 300, max_size: int = 2048):
        self._ttl = ttl
        self._max_size = max_size
        # OrderedDict-like behaviour using a plain dict (Python 3.7+ preserves
        # insertion order). On access we move the key to the end to track
        # recency.
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        # Mark as recently used by re-inserting at the end.
        del self._store[key]
        self._store[key] = (ts, value)
        return value

    def set(self, key: str, value: Any) -> None:
        if key in self._store:
            del self._store[key]
        self._store[key] = (time.time(), value)
        # Evict the oldest (front of dict) until we're back under the cap.
        while len(self._store) > self._max_size:
            oldest_key = next(iter(self._store))
            del self._store[oldest_key]
