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
    """Simple in-memory TTL cache."""

    def __init__(self, ttl: float = 300):
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)
