import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager

import httpx

from mtg_commander_mcp import __version__
from mtg_commander_mcp.utils import Cache, discard_async_client

logger = logging.getLogger(__name__)


class ScryfallError(Exception):
    pass


class ScryfallClient:
    BASE_URL = "https://api.scryfall.com"
    HEADERS = {
        "User-Agent": f"MTGCommanderMCP/{__version__}",
        "Accept": "application/json",
    }
    # Scryfall asks for 50-100ms between requests
    REQUEST_DELAY = 0.1
    # Hard ceiling on one logical API call, retries included. Well under any
    # MCP host timeout, so a wedged request surfaces as an explicit error the
    # caller can react to instead of a silent multi-minute hang.
    TOTAL_DEADLINE = 45.0
    # A 429's Retry-After is honored but capped: Cloudflare occasionally sends
    # multi-minute values, and sleeping that long inside a tool call is
    # indistinguishable from a hang to the caller.
    MAX_RETRY_WAIT = 10.0
    # 5 attempts with backoff that honors (capped) ``Retry-After``. The bigger
    # budget matters when a long serial scan (cheapest-printing across a 50+
    # card buy list) hits a transient 429 burst — 3 attempts often failed for
    # ~20% of cards.
    MAX_ATTEMPTS = 5
    # How long past a get_collection_budgeted budget an in-flight batch may run.
    BUDGET_GRACE = 10.0

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        # 30-minute TTL. Pricing data shifts on roughly a daily cadence
        # (TCGPlayer market checks every few hours), and the cheapest-
        # printing scan now issues one search per unique buy-list card —
        # without aggressive caching, a fresh pull/buy run takes
        # 2+ minutes. 30min keeps repeat calls within a session free.
        self._cache = Cache(ttl=1800)
        self._last_request = 0.0
        # Serializes the "check elapsed → sleep → mark sent" critical section
        # so concurrent coroutines can't all observe stale `_last_request` and
        # fire in lockstep. Without this, fan-out tools (price_deck) burst N
        # requests simultaneously and trip Scryfall's per-IP 429.
        self._throttle_lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers=self.HEADERS,
                timeout=15.0,
                # Cap the pool and expire idle keep-alives quickly: Scryfall
                # sits behind Cloudflare, which drops idle connections
                # server-side; reusing a half-closed one costs a 15s stall.
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    def _reset_client(self) -> None:
        """Discard the shared client so the next request builds a fresh pool.

        A request cancelled mid-flight (MCP host giving up on a slow call) or
        killed at the transport layer can leave httpx's connection pool in a
        wedged state. Before this existed, that state persisted for the life
        of the process: every later Scryfall call hung silently until the MCP
        server was restarted.
        """
        client, self._client = self._client, None
        if client is not None:
            logger.warning("Resetting Scryfall HTTP client (pool may be wedged)")
            discard_async_client(client)

    @asynccontextmanager
    async def _deadline(self, limit: float | None = None):
        """Bound a logical call to TOTAL_DEADLINE (or ``limit``) and self-heal on failure."""
        try:
            async with asyncio.timeout(min(limit or self.TOTAL_DEADLINE, self.TOTAL_DEADLINE)):
                yield
        except TimeoutError as e:
            self._reset_client()
            raise ScryfallError(
                f"Scryfall did not answer within {self.TOTAL_DEADLINE:.0f}s; "
                "the connection was reset — retry the call"
            ) from e
        except asyncio.CancelledError:
            # The host gave up on us and is tearing the request down
            # mid-transfer; don't trust the pool afterwards.
            self._reset_client()
            raise

    async def _throttle(self) -> None:
        """Enforce REQUEST_DELAY between Scryfall hits across all coroutines."""
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.REQUEST_DELAY:
                await asyncio.sleep(self.REQUEST_DELAY - elapsed)
            self._last_request = time.monotonic()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """One Scryfall call with throttling, bounded 429 retries, and
        transport-level failures converted to ``ScryfallError``.

        Callers must wrap this in ``self._deadline()`` so the retry loop
        can't outlive the per-call budget.
        """
        for attempt in range(self.MAX_ATTEMPTS):
            await self._throttle()
            # Re-resolved each attempt: an error handler may have reset it.
            client = self._get_client()

            try:
                resp = await client.request(
                    method, path, params=params, json=json_body
                )

                if resp.status_code == 429:
                    retry_after_hdr = resp.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else None
                    except ValueError:
                        retry_after = None
                    wait = min(
                        retry_after if retry_after else (1.0 + attempt * 2),
                        self.MAX_RETRY_WAIT,
                    )
                    logger.info(
                        "Scryfall 429 on %s (attempt %d); waiting %.1fs",
                        path, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    try:
                        detail = e.response.json().get("details", "Not found")
                    except ValueError:
                        detail = "Not found"
                    raise ScryfallError(detail) from e
                raise ScryfallError(f"Scryfall API error: {e.response.status_code}") from e
            except httpx.TimeoutException as e:
                self._reset_client()
                raise ScryfallError("Scryfall request timed out") from e
            except httpx.TransportError as e:
                self._reset_client()
                raise ScryfallError(
                    f"Scryfall connection error: {type(e).__name__}"
                ) from e

        raise ScryfallError("Scryfall rate limit exceeded after retries")

    async def _fetch(self, path: str, params: dict | None = None) -> dict:
        cache_key = f"{path}:{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with self._deadline():
            data = await self._request_json("GET", path, params=params)

        self._cache.set(cache_key, data)
        return data

    async def get_card(self, name: str) -> dict:
        """Look up a card by name (fuzzy matching)."""
        data = await self._fetch("/cards/named", params={"fuzzy": name})

        # DFCs and split cards store face data in card_faces
        faces = data.get("card_faces", [])
        front_face = faces[0] if faces else {}

        # Use top-level fields if present, fall back to front face
        mana_cost = data.get("mana_cost") or front_face.get("mana_cost")
        oracle_text = data.get("oracle_text") or front_face.get("oracle_text")

        # Image: top-level for single-faced, card_faces for DFCs
        image_uris = data.get("image_uris")
        if image_uris:
            image = image_uris.get("normal")
        elif front_face.get("image_uris"):
            image = front_face["image_uris"].get("normal")
        else:
            image = None

        # For DFCs, include both faces' oracle text
        if len(faces) > 1:
            face_texts = []
            for face in faces:
                face_name = face.get("name", "")
                face_text = face.get("oracle_text", "")
                if face_text:
                    face_texts.append(f"[{face_name}] {face_text}")
            if face_texts:
                oracle_text = "\n---\n".join(face_texts)

        return {
            "name": data.get("name"),
            "mana_cost": mana_cost,
            "cmc": data.get("cmc"),
            "type_line": data.get("type_line"),
            "oracle_text": oracle_text,
            "colors": data.get("colors"),
            "color_identity": data.get("color_identity"),
            "keywords": data.get("keywords"),
            "legalities": data.get("legalities"),
            "power": data.get("power") or front_face.get("power"),
            "toughness": data.get("toughness") or front_face.get("toughness"),
            "rarity": data.get("rarity"),
            "set_name": data.get("set_name"),
            "prices": data.get("prices"),
            "purchase_uris": data.get("purchase_uris"),
            "scryfall_uri": data.get("scryfall_uri"),
            "image_uris": image,
            "edhrec_rank": data.get("edhrec_rank"),
            "is_legendary": "Legendary" in data.get("type_line", ""),
            "is_creature": "Creature" in data.get("type_line", ""),
        }

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search cards using Scryfall syntax."""
        data = await self._fetch("/cards/search", params={"q": query, "order": "edhrec"})
        cards = []
        for card in data.get("data", [])[:limit]:
            cards.append({
                "name": card.get("name"),
                "mana_cost": card.get("mana_cost"),
                "cmc": card.get("cmc"),
                "type_line": card.get("type_line"),
                "oracle_text": card.get("oracle_text"),
                "prices": card.get("prices"),
                "edhrec_rank": card.get("edhrec_rank"),
                "image_uris": card.get("image_uris", {}).get("normal") if card.get("image_uris") else None,
            })
        return cards

    async def get_game_changers(self) -> set[str]:
        """Names of all Commander Game Changers (Scryfall ``is:gamechanger``).

        Lowercased, and DFC/split cards also contribute their front-face name
        so decklist names match either way. Cached with the client's default TTL.
        """
        cached = self._cache.get("game_changers")
        if cached is not None:
            return cached

        names: set[str] = set()
        path, params = "/cards/search", {"q": "is:gamechanger", "unique": "cards"}
        for _ in range(5):  # list is ~50 cards; 175/page, so 1 page in practice
            async with self._deadline():
                data = await self._request_json("GET", path, params=params)
            for card in data.get("data", []):
                name = (card.get("name") or "").lower()
                if name:
                    names.add(name)
                    names.add(name.split(" // ")[0])
            next_page = data.get("next_page")
            if not data.get("has_more") or not next_page:
                break
            path, params = next_page.replace(self.BASE_URL, ""), None

        if names:
            self._cache.set("game_changers", names)
        return names

    async def get_rulings(self, name: str) -> list[dict]:
        """Get official rulings for a card by name."""
        # First get the card to find its ID
        card_data = await self._fetch("/cards/named", params={"fuzzy": name})
        rulings_uri = card_data.get("rulings_uri", "")
        if not rulings_uri:
            return []

        # Fetch rulings using the full URI
        path = rulings_uri.replace(self.BASE_URL, "")
        data = await self._fetch(path)
        return [
            {
                "date": r.get("published_at"),
                "comment": r.get("comment"),
                "source": r.get("source"),
            }
            for r in data.get("data", [])
        ]

    @staticmethod
    def _names_key(prefix: str, names) -> str:
        """Compact cache key for a set of names (avoids stringifying 10k names)."""
        digest = hashlib.sha1("\x00".join(sorted(names)).encode()).hexdigest()
        return f"{prefix}:{digest}"

    @staticmethod
    def _identifiers(batch: list[str]) -> list[dict]:
        """Identifiers for /cards/collection. Scryfall exact-matches a single
        face name, so 'Bedeck // Bedazzle' misses while 'Bedeck' hits; send
        the front face for split/DFC names (the response is mapped back to
        the full input name by the front-face fallback in ``_collection``)."""
        fronts = dict.fromkeys(n.split(" // ")[0] for n in batch)
        return [{"name": n} for n in fronts]

    async def get_collection(
        self, names: list[str]
    ) -> tuple[dict[str, dict], list[str]]:
        """Batch-look up cards by name via /cards/collection.

        Returns ``(found, not_found)`` where ``found`` maps the input name to
        the card's full Scryfall data (including a ``prices`` subdict) and
        ``not_found`` is the list of input names Scryfall couldn't resolve via
        exact match. The caller can route ``not_found`` through the per-card
        fuzzy fallback (``get_card_price``) which handles apostrophe / accent
        / casing edge cases that exact-match would miss
        (e.g. "Jotun Grunt" vs "Jötun Grunt").

        Up to 75 names per HTTP request, so a 100-card deck costs 2 round-
        trips instead of 100 fuzzy lookups.

        Results are cached for ``Cache.ttl`` keyed on the input name set, so
        repeating the same `price_deck` call within that window is free.
        """
        found, not_found, _ = await self._collection(names, None)
        return found, not_found

    async def get_collection_budgeted(
        self, names: list[str], time_budget: float
    ) -> tuple[dict[str, dict], list[str], list[str]]:
        """Like ``get_collection`` but stops starting new batches after
        ``time_budget`` seconds; returns ``(found, not_found, deferred)``.

        ``deferred`` holds names in batches that were skipped (budget spent,
        or Scryfall started failing after some progress). Each batch is
        cached on its own, so a repeat call resumes where this one stopped.
        Needed for 10k-card collections (~130 batches), which can't finish
        inside one MCP call.
        """
        return await self._collection(names, time_budget)

    async def _collection(
        self, names: list[str], time_budget: float | None
    ) -> tuple[dict[str, dict], list[str], list[str]]:
        if not names:
            return {}, [], []

        # Cache is keyed on the unordered set of names so re-pricing the
        # same deck (or two decks with overlapping cards re-issued in the
        # same window) skips the POSTs.
        cache_key = self._names_key("collection", names)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return (*cached, [])

        result: dict[str, dict] = {}
        not_found: list[str] = []
        not_found_set: set[str] = set()
        deferred: list[str] = []
        # Lowercase->original lookup so we can map Scryfall's canonicalized
        # response name back to whatever case the caller passed in.
        name_lookup = {n.lower(): n for n in names}
        valid_inputs = set(name_lookup.values())
        # Front-face -> every input sharing it, so 'Fire' and 'Fire // Ice'
        # both resolve from the single card Scryfall returns for them.
        by_front: dict[str, list[str]] = {}
        for n in names:
            by_front.setdefault(n.split(" // ")[0].lower(), []).append(n)

        started = time.monotonic()
        budgeted = time_budget is not None
        if budgeted:
            # Stable order so per-batch cache keys line up across calls.
            names = sorted(names)
        batches_done = 0
        for batch_start in range(0, len(names), 75):
            batch = names[batch_start : batch_start + 75]
            batch_key = self._names_key("collection-batch", batch)

            data = self._cache.get(batch_key) if budgeted else None
            if data is None:
                elapsed = time.monotonic() - started
                if budgeted and elapsed > time_budget:
                    deferred.extend(batch)
                    continue
                # Deadline per batch (not per call): a large collection can
                # legitimately take many batches, but no single batch should
                # be able to hang the tool. A budgeted batch started just
                # under the budget gets only the grace window to finish.
                limit = (
                    max(5.0, time_budget + self.BUDGET_GRACE - elapsed)
                    if budgeted else None
                )
                try:
                    async with self._deadline(limit):
                        data = await self._request_json(
                            "POST",
                            "/cards/collection",
                            json_body={"identifiers": self._identifiers(batch)},
                        )
                except ScryfallError as e:
                    # Budgeted callers keep what they've resolved so far; a
                    # rate-limit burst shouldn't discard 100 good batches.
                    if not budgeted or batches_done == 0:
                        raise
                    logger.warning("Stopping collection scan early: %s", e)
                    deferred.extend(names[batch_start:])
                    break
                if budgeted:
                    self._cache.set(batch_key, data)
            batches_done += 1

            matched_in_batch: set[str] = set()
            for card in data.get("data", []):
                # Match by lowercase name OR front-face name for DFCs
                returned = card.get("name", "")
                keys = {name_lookup.get(returned.lower())}
                keys.update(by_front.get(returned.split(" // ")[0].lower(), []))
                for key in keys - {None}:
                    result[key] = card
                    matched_in_batch.add(key)

            # Trust Scryfall's own `not_found` list rather than re-deriving it
            # from the response — it carries the exact identifiers that didn't
            # resolve, so there's no risk of false-positives from name
            # canonicalization quirks.
            for nf in data.get("not_found", []):
                nf_name = nf.get("name") if isinstance(nf, dict) else None
                if nf_name and nf_name in valid_inputs and nf_name not in not_found_set:
                    not_found.append(nf_name)
                    not_found_set.add(nf_name)
            # Belt-and-suspenders: any input from this batch that didn't match
            # AND wasn't in `not_found` (e.g. Scryfall returned a different
            # canonical form we couldn't map) goes through fallback too.
            for name in batch:
                if name not in matched_in_batch and name not in not_found_set:
                    not_found.append(name)
                    not_found_set.add(name)

        if not deferred:
            self._cache.set(cache_key, (result, not_found))
        return result, not_found, deferred

    async def get_card_price(self, name: str) -> dict:
        """Get pricing info for a card, finding a printing with prices if the default lacks them."""
        data = await self._fetch("/cards/named", params={"fuzzy": name})
        prices = data.get("prices", {})

        # If the default printing has no USD price, check other printings
        if not prices.get("usd"):
            prints_uri = data.get("prints_search_uri", "")
            if prints_uri:
                try:
                    path = prints_uri.replace(self.BASE_URL, "")
                    prints_data = await self._fetch(path)
                    for printing in prints_data.get("data", []):
                        p = printing.get("prices", {})
                        if p.get("usd"):
                            prices = p
                            break
                except ScryfallError as e:
                    # Prints-search fallback failed; the caller will get the
                    # default printing's (price-less) data and report the card
                    # as missing. Log so the operator can tell whether it's
                    # rate-limit pressure vs a genuinely missing oracle.
                    logger.warning(
                        "Scryfall prints-search fallback failed for %r: %s",
                        name, e,
                    )

        return {
            "name": data.get("name"),
            "prices": prices,
            "purchase_uris": data.get("purchase_uris"),
        }
