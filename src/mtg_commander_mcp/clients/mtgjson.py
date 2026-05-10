"""MTGJSON daily-price snapshot client.

Bulk-data backend for buy-list pricing. Two cached artifacts at
``$XDG_CACHE_HOME/mtg-commander-mcp/`` (defaults to
``~/.cache/mtg-commander-mcp/``):

* **``prices.sqlite``** — verbatim copy of MTGJSON's
  ``AllPricesToday.sqlite`` (≈132 MB). Single ``prices`` table:
  ``(uuid TEXT, date TEXT, source TEXT, provider TEXT,
  priceType TEXT, finish TEXT, price REAL, currency TEXT)``
  with index on ``uuid``. Daily snapshot of every printing's price
  across TCGplayer, Card Kingdom, Cardmarket, Manapool, Cardhoarder.
* **``identifiers.sqlite``** — locally built from MTGJSON's
  ``AllIdentifiers.json.bz2`` (≈145 MB compressed) once. Single
  table ``scryfall_to_mtgjson(scryfall_id, mtgjson_uuid)`` with
  index on ``scryfall_id``. Maps Scryfall UUIDs (what our other
  tools already carry) to MTGJSON UUIDs (what ``prices.sqlite`` is
  keyed by). Rebuilt on demand when a lookup misses — new printings
  appear weekly, so monthly rebuilds are sufficient.

Refresh policy:

* ``prices.sqlite`` is re-downloaded if older than 24h.
* ``identifiers.sqlite`` is built once and refreshed only when a
  Scryfall ID lookup misses (signaling MTGJSON has indexed a card
  we don't know about yet), or on explicit ``refresh_pricing_data``
  tool call.

The client is **opportunistic**: if the cache is fresh, lookups are
~1ms local SQLite reads (multi-thousand-card buy lists in
milliseconds). If the cache is missing or stale, ``lookup_price``
returns ``None`` and the caller falls through to its existing
network path (Scryfall ``/cards/collection``), with a background
refresh kicked off so subsequent calls are fast.

There is no API key, no auth, no rate limit. MTGJSON is a free
community resource; we still set a polite User-Agent.
"""

from __future__ import annotations

import asyncio
import bz2
import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import httpx

from mtg_commander_mcp import __version__

logger = logging.getLogger(__name__)


class MTGJSONError(Exception):
    pass


PRICES_URL = "https://mtgjson.com/api/v5/AllPricesToday.sqlite"
IDENTIFIERS_URL = "https://mtgjson.com/api/v5/AllIdentifiers.json.bz2"

# Prices roll over once a day at ~06:10 UTC; 24h is the natural cadence.
PRICES_MAX_AGE_SECONDS = 24 * 3600

# Identifiers change much slower (new sets only). Refresh once a month
# unless a lookup miss forces a rebuild sooner.
IDENTIFIERS_MAX_AGE_SECONDS = 30 * 24 * 3600


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "mtg-commander-mcp"


class MTGJSONClient:
    """Bulk-pricing client. Stateful across MCP requests within one
    server process — owns the cache directory and the connection
    handles to the two SQLite files. Thread-safe under asyncio
    (single-process, no shared mutable state outside SQLite's own
    serialization).
    """

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or _cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._prices_path = self._cache_dir / "prices.sqlite"
        self._identifiers_path = self._cache_dir / "identifiers.sqlite"
        # Serialize concurrent downloads so two pricing calls don't both
        # kick off a 132 MB fetch on a cold cache.
        self._download_lock = asyncio.Lock()
        # In-process connection handles. SQLite connections are not
        # thread-safe across event-loop threads but we're single-loop here.
        self._prices_conn: sqlite3.Connection | None = None
        self._identifiers_conn: sqlite3.Connection | None = None
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Both caches exist and prices are fresh enough to use."""
        return (
            self._prices_path.exists()
            and self._identifiers_path.exists()
            and self._prices_age_seconds() <= PRICES_MAX_AGE_SECONDS
        )

    def cache_info(self) -> dict:
        """Snapshot of cache state for diagnostic / refresh tools."""
        return {
            "cache_dir": str(self._cache_dir),
            "prices_present": self._prices_path.exists(),
            "prices_path": str(self._prices_path),
            "prices_size_bytes": (
                self._prices_path.stat().st_size if self._prices_path.exists() else 0
            ),
            "prices_age_seconds": (
                int(self._prices_age_seconds())
                if self._prices_path.exists()
                else None
            ),
            "prices_fresh": (
                self._prices_path.exists()
                and self._prices_age_seconds() <= PRICES_MAX_AGE_SECONDS
            ),
            "identifiers_present": self._identifiers_path.exists(),
            "identifiers_path": str(self._identifiers_path),
            "identifiers_size_bytes": (
                self._identifiers_path.stat().st_size
                if self._identifiers_path.exists()
                else 0
            ),
            "identifiers_age_seconds": (
                int(time.time() - self._identifiers_path.stat().st_mtime)
                if self._identifiers_path.exists()
                else None
            ),
        }

    async def ensure_fresh(self, *, force: bool = False) -> dict:
        """Refresh both caches if needed. Returns a summary suitable
        for an MCP tool response.
        """
        async with self._download_lock:
            summary: dict = {"prices": None, "identifiers": None}

            if (
                force
                or not self._prices_path.exists()
                or self._prices_age_seconds() > PRICES_MAX_AGE_SECONDS
            ):
                summary["prices"] = await self._download_prices()
            else:
                summary["prices"] = {
                    "action": "skipped",
                    "reason": "fresh",
                    "age_seconds": int(self._prices_age_seconds()),
                }

            if (
                force
                or not self._identifiers_path.exists()
                or (
                    time.time() - self._identifiers_path.stat().st_mtime
                ) > IDENTIFIERS_MAX_AGE_SECONDS
            ):
                summary["identifiers"] = await self._download_identifiers()
            else:
                summary["identifiers"] = {
                    "action": "skipped",
                    "reason": "fresh",
                    "age_seconds": int(
                        time.time() - self._identifiers_path.stat().st_mtime
                    ),
                }

            # Reset connections so subsequent queries see the new files.
            self._close_connections()
            return summary

    def lookup_price(self, scryfall_id: str) -> dict | None:
        """Return prices for a Scryfall printing UUID.

        Output shape:
            {
              "mtgjson_uuid": "...",
              "providers": {
                "tcgplayer": {"normal_usd": 0.45, "foil_usd": 4.50, ...},
                "cardkingdom": {...},
                "cardmarket": {"normal_eur": 0.30, "foil_eur": ...},
                "manapool": {...},
              },
              "snapshot_date": "2026-05-10",
            }

        Returns None if either cache is missing OR the Scryfall ID
        isn't in the identifier map. The caller is expected to fall
        through to a network pricing path on None.
        """
        if not self.is_available():
            return None

        ids_conn = self._get_identifiers_conn()
        if ids_conn is None:
            return None

        row = ids_conn.execute(
            "SELECT mtgjson_uuid FROM scryfall_to_mtgjson WHERE scryfall_id = ?",
            (scryfall_id,),
        ).fetchone()
        if row is None:
            return None
        mtgjson_uuid = row[0]

        prices_conn = self._get_prices_conn()
        if prices_conn is None:
            return None

        rows = prices_conn.execute(
            "SELECT provider, finish, price, currency, date "
            "FROM prices "
            "WHERE uuid = ? AND source = 'paper' AND priceType = 'retail'",
            (mtgjson_uuid,),
        ).fetchall()
        if not rows:
            return None

        providers: dict[str, dict] = {}
        snapshot_date = None
        for provider, finish, price, currency, date in rows:
            # Normalize key: tcgplayer's USD normal price → "normal_usd".
            currency_lower = (currency or "").lower()
            key = f"{finish}_{currency_lower}"
            providers.setdefault(provider, {})[key] = float(price)
            snapshot_date = date  # all rows in a single snapshot share a date

        return {
            "mtgjson_uuid": mtgjson_uuid,
            "providers": providers,
            "snapshot_date": snapshot_date,
        }

    def best_retail_price(
        self, scryfall_id: str, *, prefer: str = "tcgplayer"
    ) -> tuple[float, str, str] | None:
        """Return ``(price, provider, currency_finish)`` for a single
        best paper-retail price.

        Preference order: the named ``prefer`` provider first (default
        TCGplayer), then any USD provider, then any provider at all.
        Within a provider, lowest of (normal_usd, foil_usd) wins so
        precon-foil-cheaper-than-promo-nonfoil cases are caught
        automatically.
        """
        data = self.lookup_price(scryfall_id)
        if data is None:
            return None
        providers = data["providers"]

        def _cheapest(provider_data: dict) -> tuple[float, str] | None:
            usd_candidates = [
                (provider_data["normal_usd"], "normal_usd")
                if "normal_usd" in provider_data
                else None,
                (provider_data["foil_usd"], "foil_usd")
                if "foil_usd" in provider_data
                else None,
                (provider_data["etched_usd"], "etched_usd")
                if "etched_usd" in provider_data
                else None,
            ]
            usd_candidates = [c for c in usd_candidates if c is not None]
            if usd_candidates:
                return min(usd_candidates)
            return None

        if prefer in providers:
            best = _cheapest(providers[prefer])
            if best is not None:
                return best[0], prefer, best[1]

        # Fall back to any USD provider.
        for provider in ("tcgplayer", "cardkingdom", "manapool"):
            if provider == prefer:
                continue
            if provider in providers:
                best = _cheapest(providers[provider])
                if best is not None:
                    return best[0], provider, best[1]

        return None

    def close(self) -> None:
        """Close cached SQLite connections and the HTTP client. Safe
        to call multiple times."""
        self._close_connections()
        if self._http is not None:
            try:
                # httpx AsyncClient.aclose must be awaited; the sync
                # close() just deletes the reference.
                self._http = None
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prices_age_seconds(self) -> float:
        if not self._prices_path.exists():
            return float("inf")
        return time.time() - self._prices_path.stat().st_mtime

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
                headers={
                    "User-Agent": f"MTGCommanderMCP/{__version__}",
                    "Accept-Encoding": "gzip, bzip2",
                },
                follow_redirects=True,
            )
        return self._http

    def _get_prices_conn(self) -> sqlite3.Connection | None:
        if self._prices_conn is not None:
            return self._prices_conn
        if not self._prices_path.exists():
            return None
        # check_same_thread=False is safe here because asyncio runs
        # on a single thread and we own all the references.
        self._prices_conn = sqlite3.connect(
            self._prices_path, check_same_thread=False
        )
        return self._prices_conn

    def _get_identifiers_conn(self) -> sqlite3.Connection | None:
        if self._identifiers_conn is not None:
            return self._identifiers_conn
        if not self._identifiers_path.exists():
            return None
        self._identifiers_conn = sqlite3.connect(
            self._identifiers_path, check_same_thread=False
        )
        return self._identifiers_conn

    def _close_connections(self) -> None:
        if self._prices_conn is not None:
            try:
                self._prices_conn.close()
            except Exception:
                pass
            self._prices_conn = None
        if self._identifiers_conn is not None:
            try:
                self._identifiers_conn.close()
            except Exception:
                pass
            self._identifiers_conn = None

    async def _download_prices(self) -> dict:
        """Fetch AllPricesToday.sqlite to a tempfile, validate, swap in."""
        t0 = time.monotonic()
        logger.info("MTGJSON: downloading prices from %s", PRICES_URL)
        tmp = self._cache_dir / f".prices.tmp.{os.getpid()}.sqlite"
        try:
            await self._stream_to_file(PRICES_URL, tmp)
            # Sanity-check the downloaded file before swapping. A
            # truncated download would crash the next query; better to
            # leave the old cache in place.
            try:
                conn = sqlite3.connect(tmp)
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM prices"
                ).fetchone()[0]
                conn.close()
            except sqlite3.DatabaseError as e:
                tmp.unlink(missing_ok=True)
                raise MTGJSONError(
                    f"downloaded prices file failed sanity check: {e}"
                ) from e
            if cnt < 100_000:
                tmp.unlink(missing_ok=True)
                raise MTGJSONError(
                    f"downloaded prices file has only {cnt} rows; refusing to swap"
                )
            os.replace(tmp, self._prices_path)
            elapsed = time.monotonic() - t0
            logger.info(
                "MTGJSON: prices refreshed in %.1fs (%d rows, %.1f MB)",
                elapsed,
                cnt,
                self._prices_path.stat().st_size / 1024 / 1024,
            )
            return {
                "action": "downloaded",
                "rows": cnt,
                "size_bytes": self._prices_path.stat().st_size,
                "elapsed_seconds": round(elapsed, 2),
            }
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise MTGJSONError(f"prices download failed: {e}") from e

    async def _download_identifiers(self) -> dict:
        """Fetch AllIdentifiers.json.bz2, extract scryfall_id →
        mtgjson_uuid pairs, build a SQLite mapping. Total work is
        ~30s wall (dominated by the 145 MB download).
        """
        t0 = time.monotonic()
        logger.info("MTGJSON: downloading identifiers from %s", IDENTIFIERS_URL)
        tmp_archive = self._cache_dir / f".identifiers.tmp.{os.getpid()}.json.bz2"
        tmp_db = self._cache_dir / f".identifiers.tmp.{os.getpid()}.sqlite"
        try:
            await self._stream_to_file(IDENTIFIERS_URL, tmp_archive)

            # Stream-parse bz2 → JSON in-memory. We can't avoid the
            # full JSON load (no JSON Lines variant available), so
            # peak RSS is ~600 MB transient. Acceptable for a once-
            # a-month refresh.
            with bz2.open(tmp_archive, "rt", encoding="utf-8") as f:
                data = json.load(f)

            cards = data.get("data") or {}
            if len(cards) < 10_000:
                raise MTGJSONError(
                    f"identifiers archive parsed to only {len(cards)} cards; refusing"
                )

            tmp_db.unlink(missing_ok=True)
            conn = sqlite3.connect(tmp_db)
            try:
                conn.execute("PRAGMA journal_mode = OFF")
                conn.execute("PRAGMA synchronous = OFF")
                conn.execute(
                    "CREATE TABLE scryfall_to_mtgjson "
                    "(scryfall_id TEXT PRIMARY KEY, mtgjson_uuid TEXT NOT NULL)"
                )
                rows = []
                for mtgjson_uuid, entry in cards.items():
                    sid = (entry.get("identifiers") or {}).get("scryfallId")
                    if sid:
                        rows.append((sid, mtgjson_uuid))
                # Use INSERT OR IGNORE: MTGJSON has rare edge-cases
                # where two MTGJSON UUIDs map to the same scryfallId
                # (alternative-art / proxy promos). First write wins;
                # the alternative just won't be price-able via this
                # mapping and falls back to Scryfall.
                conn.executemany(
                    "INSERT OR IGNORE INTO scryfall_to_mtgjson VALUES (?, ?)",
                    rows,
                )
                conn.execute(
                    "CREATE INDEX idx_scryfall_id ON scryfall_to_mtgjson(scryfall_id)"
                )
                conn.commit()
            finally:
                conn.close()

            os.replace(tmp_db, self._identifiers_path)
            tmp_archive.unlink(missing_ok=True)
            elapsed = time.monotonic() - t0
            logger.info(
                "MTGJSON: identifiers rebuilt in %.1fs (%d mappings)",
                elapsed,
                len(rows),
            )
            return {
                "action": "downloaded",
                "mappings": len(rows),
                "size_bytes": self._identifiers_path.stat().st_size,
                "elapsed_seconds": round(elapsed, 2),
            }
        except (httpx.HTTPError, OSError) as e:
            tmp_archive.unlink(missing_ok=True)
            tmp_db.unlink(missing_ok=True)
            raise MTGJSONError(f"identifiers download failed: {e}") from e

    async def _stream_to_file(self, url: str, dest: Path) -> None:
        """Stream a URL into a file. Manual chunked write so we don't
        buffer 132 MB in RAM."""
        http = self._get_http()
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
