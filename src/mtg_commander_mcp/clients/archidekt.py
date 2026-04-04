import re

import httpx

from mtg_commander_mcp.utils import Cache


class ArchidektError(Exception):
    pass


class ArchidektClient:
    BASE_URL = "https://archidekt.com/api"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=300)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": "MTGCommanderMCP/0.1.0",
                    "Accept": "application/json",
                },
            )
        return self._client

    @staticmethod
    def extract_deck_id(url: str) -> str:
        """Extract deck ID from an Archidekt URL.

        Handles: archidekt.com/decks/12345/...
        """
        match = re.search(r"archidekt\.com/decks/(\d+)", url)
        if not match:
            raise ArchidektError(f"Could not extract deck ID from URL: {url}")
        return match.group(1)

    async def get_deck(self, url: str) -> dict:
        """Fetch a deck by Archidekt URL."""
        deck_id = self.extract_deck_id(url)
        cache_key = f"deck:{deck_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._get_client()
        try:
            resp = await client.get(f"{self.BASE_URL}/decks/{deck_id}/")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ArchidektError(f"Deck not found: {deck_id}") from e
            raise ArchidektError(f"Archidekt error: {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            raise ArchidektError("Archidekt request timed out") from e

        result = self._parse_deck(data)
        self._cache.set(cache_key, result)
        return result

    def _parse_deck(self, data: dict) -> dict:
        """Parse Archidekt deck response into a normalized format."""
        # Group cards by category
        categories: dict[str, list[dict]] = {}
        for entry in data.get("cards", []):
            card_data = entry.get("card", {})
            oracle = card_data.get("oracleCard") or {}
            card_categories = entry.get("categories") or ["Uncategorized"]
            quantity = entry.get("quantity", 1)

            card = {
                "name": oracle.get("name") or card_data.get("displayName") or "Unknown",
                "quantity": quantity,
                "cmc": oracle.get("cmc") or card_data.get("cmc"),
                "colors": oracle.get("colors") or card_data.get("colors"),
                "color_identity": oracle.get("colorIdentity") or card_data.get("colorIdentity"),
                "type_line": oracle.get("typeLine"),
                "edition": (card_data.get("edition") or {}).get("editioncode"),
            }

            for cat in card_categories:
                categories.setdefault(cat, []).append(card)

        return {
            "name": data.get("name", "Unknown Deck"),
            "format": data.get("deckFormat"),
            "owner": data.get("owner", {}).get("username"),
            "categories": categories,
            "card_count": sum(
                entry.get("quantity", 1) for entry in data.get("cards", [])
            ),
        }
