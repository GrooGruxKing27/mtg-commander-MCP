import asyncio

import httpx

from mtg_commander_mcp.utils import Cache


class ScryfallError(Exception):
    pass


class ScryfallClient:
    BASE_URL = "https://api.scryfall.com"
    HEADERS = {
        "User-Agent": "MTGCommanderMCP/0.1.0",
        "Accept": "application/json",
    }
    # Scryfall asks for 50-100ms between requests
    REQUEST_DELAY = 0.1

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=300)
        self._last_request = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers=self.HEADERS,
                timeout=15.0,
            )
        return self._client

    async def _fetch(self, path: str, params: dict | None = None) -> dict:
        cache_key = f"{path}:{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._get_client()

        for attempt in range(3):
            # Rate limiting
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request
            if elapsed < self.REQUEST_DELAY:
                await asyncio.sleep(self.REQUEST_DELAY - elapsed)

            try:
                resp = await client.get(path, params=params)
                self._last_request = asyncio.get_event_loop().time()

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
                except ScryfallError:
                    pass

        return {
            "name": data.get("name"),
            "prices": prices,
            "purchase_uris": data.get("purchase_uris"),
        }
