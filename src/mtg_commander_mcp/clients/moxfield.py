import re
from functools import partial

import cloudscraper

from mtg_commander_mcp.utils import Cache


class MoxfieldError(Exception):
    pass


class MoxfieldClient:
    API_URL = "https://api.moxfield.com/v2/decks/all"

    def __init__(self):
        self._cache = Cache(ttl=300)
        self._scraper = None

    def _get_scraper(self) -> cloudscraper.CloudScraper:
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper()
        return self._scraper

    @staticmethod
    def extract_deck_id(url: str) -> str:
        """Extract deck public ID from a Moxfield URL.

        Handles: moxfield.com/decks/oEWXWHM5eEGMmopExLWRCA
        """
        match = re.search(r"moxfield\.com/decks/([a-zA-Z0-9_-]+)", url)
        if not match:
            raise MoxfieldError(f"Could not extract deck ID from URL: {url}")
        return match.group(1)

    async def get_deck(self, url: str) -> dict:
        """Fetch a deck by Moxfield URL.

        Uses cloudscraper to bypass Cloudflare protection.
        Runs synchronous requests in a thread executor.
        """
        import asyncio

        deck_id = self.extract_deck_id(url)
        cache_key = f"deck:{deck_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None, partial(self._fetch_sync, deck_id)
            )
        except Exception as e:
            raise MoxfieldError(f"Failed to fetch Moxfield deck: {e}") from e

        result = self._parse_deck(resp)
        self._cache.set(cache_key, result)
        return result

    def _fetch_sync(self, deck_id: str) -> dict:
        scraper = self._get_scraper()
        resp = scraper.get(f"{self.API_URL}/{deck_id}")
        if resp.status_code == 404:
            raise MoxfieldError(f"Deck not found: {deck_id}")
        if resp.status_code != 200:
            raise MoxfieldError(f"Moxfield error: {resp.status_code}")
        return resp.json()

    def _parse_deck(self, data: dict) -> dict:
        """Parse Moxfield deck response into a normalized format."""
        boards = ["mainboard", "sideboard", "commanders", "companions"]
        categories: dict[str, list[dict]] = {}
        total_cards = 0

        for board in boards:
            board_data = data.get(board, {})
            if not board_data:
                continue
            cards = []
            for card_name, entry in board_data.items():
                quantity = entry.get("quantity", 1)
                card_obj = entry.get("card", {})
                cards.append({
                    "name": card_obj.get("name", card_name),
                    "quantity": quantity,
                    "cmc": card_obj.get("cmc"),
                    "type_line": card_obj.get("type_line"),
                    "color_identity": card_obj.get("color_identity"),
                })
                total_cards += quantity
            if cards:
                cat_name = board.capitalize()
                if board == "commanders":
                    cat_name = "Commander"
                categories[cat_name] = cards

        return {
            "name": data.get("name", "Unknown Deck"),
            "format": data.get("format"),
            "owner": data.get("createdByUser", {}).get("userName"),
            "categories": categories,
            "card_count": total_cards,
        }
