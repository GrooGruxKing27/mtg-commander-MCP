import asyncio
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


# Background-close tasks for discarded httpx clients, held here so they
# aren't garbage-collected mid-close; entries remove themselves on completion.
_pending_closes: set = set()


def discard_async_client(client: Any) -> None:
    """Close an httpx.AsyncClient without awaiting it.

    Used when a client's connection pool may be wedged (request cancelled
    mid-flight by the MCP host, repeated timeouts, transport errors). The
    owner detaches its reference first so the next request builds a fresh
    pool; the old client is then closed in a background task where a hung
    close can't block anything — and where cancellation of the *caller*
    can't interrupt the close halfway.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (interpreter teardown) — let GC handle it
    task = loop.create_task(_aclose_quietly(client))
    _pending_closes.add(task)
    task.add_done_callback(_pending_closes.discard)


async def _aclose_quietly(client: Any) -> None:
    try:
        await asyncio.wait_for(client.aclose(), timeout=5.0)
    except Exception:
        pass


class Cache:
    """In-memory TTL + LRU cache.

    TTL evicts entries by age, ``max_size`` caps total entries (oldest-first
    eviction). Both bounds are needed for a long-running daemon: TTL alone
    lets the cache grow without limit if every entry is accessed within its
    window, and LRU alone lets stale data linger indefinitely.

    TTL is bound to *write* time — reads do not extend an entry's lifetime.
    Reads do mark the entry as recently used for LRU purposes, but the TTL
    clock keeps ticking from the original ``set()`` call. This is the right
    behavior for "data freshness" caches (we want stale Scryfall prices to
    expire even if they're being read constantly).
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
