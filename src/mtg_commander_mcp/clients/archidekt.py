import re

import httpx

from mtg_commander_mcp import __version__
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
                    "User-Agent": f"MTGCommanderMCP/{__version__}",
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

    # Archidekt encodes deck format as a small integer. Source: format-key
    # map in https://github.com/Nitemice/archidekt-backup-gas/blob/main/code.js
    # plus on-the-wire verification via /api/decks/v3/. Used by
    # `list_user_decks` to translate human-readable format names.
    FORMAT_CODES: dict[str, tuple[int, ...]] = {
        "standard": (1,),
        "modern": (2,),
        "commander": (3, 11, 12, 14, 17),  # EDH + 1v1 + Duel + Oathbreaker + Pauper EDH
        "edh": (3,),
        "legacy": (4,),
        "vintage": (5,),
        "pauper": (6,),
        "custom": (7,),
        "frontier": (8,),
        "future": (9,),
        "penny": (10,),
        "brawl": (13, 20),  # Brawl + Historic Brawl
        "pioneer": (15,),
        "historic": (16,),
        "alchemy": (18,),
        "explorer": (19,),
    }

    async def list_user_decks(
        self,
        username: str,
        format_filter: str | None = None,
        page_limit: int = 20,
        include_unlisted: bool = False,
    ) -> list[dict]:
        """List public decks owned by an Archidekt user.

        Endpoint: ``/api/decks/v3/?ownerUsername=<user>&ownerexact=true``.
        Returns all non-private decks visible on the user's public
        profile across every format by default (matches what shows up
        on `archidekt.com/u/<user>`).

        Pagination follows the API's `next` cursor up to ``page_limit``
        pages. The API returns ~60 decks/page regardless of the
        `pageSize` parameter we send, so the default cap covers
        ~1200 decks — power-creator scale. The loop breaks early
        when `next` is None, so the cap is effectively free for
        normal-sized accounts.

        Args:
            username: Archidekt username (case-insensitive in API).
            format_filter: human format name (e.g. "commander",
                "modern"). Maps to Archidekt's numeric `deckFormat`
                via FORMAT_CODES. None returns every format.
            page_limit: max pages to fetch (50 decks/page).
            include_unlisted: include decks marked `unlisted` (still
                public-by-URL but hidden from search). Default False.
        """
        client = self._get_client()
        results: list[dict] = []

        target_codes: set[int] | None = None
        if format_filter:
            codes = self.FORMAT_CODES.get(format_filter.lower())
            if codes is None:
                raise ArchidektError(
                    f"Unknown format_filter {format_filter!r}. "
                    f"Known: {', '.join(sorted(self.FORMAT_CODES))}"
                )
            target_codes = set(codes)

        params: dict[str, str | int] = {
            "ownerUsername": username,
            "ownerexact": "true",
            "pageSize": 50,
            "orderBy": "-updatedAt",
        }

        for _ in range(page_limit):
            try:
                resp = await client.get(f"{self.BASE_URL}/decks/v3/", params=params)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                raise ArchidektError(
                    f"Archidekt user-decks error: {e.response.status_code}"
                ) from e
            except httpx.TimeoutException as e:
                raise ArchidektError("Archidekt request timed out") from e

            entries = data.get("results") if isinstance(data, dict) else None
            if not entries:
                break

            for entry in entries:
                deck_format = entry.get("deckFormat")
                if target_codes is not None and deck_format not in target_codes:
                    continue
                if entry.get("private"):
                    continue
                if entry.get("unlisted") and not include_unlisted:
                    continue
                deck_id = entry.get("id")
                if deck_id is None:
                    continue
                results.append(
                    {
                        "id": deck_id,
                        "name": entry.get("name"),
                        "format": deck_format,
                        "size": entry.get("size"),
                        "updated_at": entry.get("updatedAt"),
                        "url": f"https://archidekt.com/decks/{deck_id}",
                    }
                )

            next_url = data.get("next")
            if not next_url:
                break
            # The `next` URL the API returns contains the cursor (`page=2`
            # etc.) — extract its query string and reuse our base URL.
            from urllib.parse import urlparse, parse_qsl

            qs = dict(parse_qsl(urlparse(next_url).query))
            params = {**params, **qs}

        return results

    def _parse_deck(self, data: dict) -> dict:
        """Parse Archidekt deck response into a normalized format."""
        # Group cards by category
        categories: dict[str, list[dict]] = {}
        for entry in data.get("cards", []):
            card_data = entry.get("card", {})
            oracle = card_data.get("oracleCard") or {}
            card_categories = entry.get("categories") or ["Uncategorized"]
            quantity = entry.get("quantity", 1)

            edition_code = (card_data.get("edition") or {}).get("editioncode")
            card = {
                "name": oracle.get("name") or card_data.get("displayName") or "Unknown",
                "quantity": quantity,
                "cmc": oracle.get("cmc") or card_data.get("cmc"),
                "colors": oracle.get("colors") or card_data.get("colors"),
                "color_identity": oracle.get("colorIdentity") or card_data.get("colorIdentity"),
                "type_line": oracle.get("typeLine"),
                "edition": edition_code,
                "set_code": edition_code.lower() if edition_code else None,
                "collector_number": card_data.get("collectorNumber"),
                "scryfall_id": card_data.get("uid"),
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
