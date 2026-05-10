import asyncio
import logging
import time

import httpx

from mtg_commander_mcp import __version__
from mtg_commander_mcp.utils import Cache

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

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=300)
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
            )
        return self._client

    async def _throttle(self) -> None:
        """Enforce REQUEST_DELAY between Scryfall hits across all coroutines."""
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.REQUEST_DELAY:
                await asyncio.sleep(self.REQUEST_DELAY - elapsed)
            self._last_request = time.monotonic()

    async def _fetch(self, path: str, params: dict | None = None) -> dict:
        cache_key = f"{path}:{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._get_client()

        for attempt in range(3):
            await self._throttle()

            try:
                resp = await client.get(path, params=params)

                # Retry on rate limit
                if resp.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue

                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    detail = e.response.json().get("details", "Not found")
                    raise ScryfallError(detail) from e
                raise ScryfallError(f"Scryfall API error: {e.response.status_code}") from e
            except httpx.TimeoutException as e:
                raise ScryfallError("Scryfall request timed out") from e

            self._cache.set(cache_key, data)
            return data

        raise ScryfallError("Scryfall rate limit exceeded after retries")

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

        Results are cached for ``Cache.ttl`` (default 5min) keyed on the input
        name set, so repeating the same `price_deck` call within that window
        is free.
        """
        if not names:
            return {}, []

        # Cache is keyed on the unordered set of names so re-pricing the
        # same deck (or two decks with overlapping cards re-issued in the
        # same window) skips the POSTs.
        cache_key = f"collection:{frozenset(names)}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._get_client()
        result: dict[str, dict] = {}
        not_found: list[str] = []
        not_found_set: set[str] = set()
        # Lowercase->original lookup so we can map Scryfall's canonicalized
        # response name back to whatever case the caller passed in.
        name_lookup = {n.lower(): n for n in names}
        valid_inputs = set(name_lookup.values())

        for batch_start in range(0, len(names), 75):
            batch = names[batch_start : batch_start + 75]
            identifiers = [{"name": n} for n in batch]

            await self._throttle()

            try:
                resp = await client.post(
                    "/cards/collection", json={"identifiers": identifiers}
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                raise ScryfallError(
                    f"Scryfall collection error: {e.response.status_code}"
                ) from e
            except httpx.TimeoutException as e:
                raise ScryfallError("Scryfall collection request timed out") from e

            matched_in_batch: set[str] = set()
            for card in data.get("data", []):
                # Match by lowercase name OR front-face name for DFCs
                returned = card.get("name", "")
                key = name_lookup.get(returned.lower())
                if key is None:
                    front = returned.split(" // ")[0]
                    key = name_lookup.get(front.lower())
                if key is not None:
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

        self._cache.set(cache_key, (result, not_found))
        return result, not_found

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
