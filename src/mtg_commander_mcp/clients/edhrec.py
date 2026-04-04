import httpx

from mtg_commander_mcp.utils import Cache, to_slug


class EDHRecError(Exception):
    pass


VALID_COLOR_FILTERS = [
    "white", "blue", "black", "red", "green", "colorless",
    "azorius", "dimir", "rakdos", "gruul", "selesnya",
    "orzhov", "izzet", "golgari", "boros", "simic",
    "esper", "grixis", "jund", "naya", "bant",
    "abzan", "jeskai", "sultai", "mardu", "temur",
    "five-color",
]


class EDHRecClient:
    BASE_URL = "https://json.edhrec.com/pages"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=300)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "MTGCommanderMCP/0.1.0"},
            )
        return self._client

    async def _fetch(self, path: str) -> dict:
        cached = self._cache.get(path)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}/{path}.json"
        client = self._get_client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise EDHRecError(f"Not found: {path}") from e
            raise EDHRecError(f"EDHRec error: {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            raise EDHRecError("EDHRec request timed out") from e

        # Handle EDHRec JSON-level redirects
        if isinstance(data, dict) and "redirect" in data:
            redirect_path = data["redirect"].strip("/")
            return await self._fetch(redirect_path)

        self._cache.set(path, data)
        return data

    def _extract_cardlists(self, data: dict, limit: int | None = None) -> list[dict]:
        """Extract card lists from EDHRec container response."""
        categories = []
        container = data.get("container", data)
        json_dict = container.get("json_dict", container)
        cardlists = json_dict.get("cardlists", [])

        for cl in cardlists:
            header = cl.get("header", "Unknown")
            cards = []
            for cv in cl.get("cardviews", []):
                card = {
                    "name": cv.get("name"),
                    "synergy": cv.get("synergy"),
                    "inclusion": cv.get("inclusion"),
                    "num_decks": cv.get("num_decks"),
                    "potential_decks": cv.get("potential_decks"),
                    "label": cv.get("label"),
                }
                # Include price if available
                prices = cv.get("prices")
                if prices:
                    card["prices"] = prices
                cards.append(card)
            if limit:
                cards = cards[:limit]
            if cards:
                categories.append({"category": header, "cards": cards})
        return categories

    async def get_commander(self, name: str, theme: str | None = None) -> dict:
        """Get commander recommendations, optionally filtered by theme."""
        slug = to_slug(name)
        path = f"commanders/{slug}/{theme}" if theme else f"commanders/{slug}"
        data = await self._fetch(path)

        # Top-level fields: header, num_decks_avg, panels, avg_price
        # Cardlists are in container.json_dict.cardlists
        result = {
            "name": data.get("header", name),
            "num_decks": data.get("num_decks_avg"),
            "avg_price": data.get("avg_price"),
        }

        # Card categories (inside container.json_dict)
        result["card_lists"] = self._extract_cardlists(data)

        # Themes (panels is top-level)
        panels = data.get("panels", {})
        taglinks = panels.get("taglinks", [])
        if taglinks:
            result["themes"] = [
                {
                    "name": t.get("value"),
                    "slug": t.get("slug"),
                    "deck_count": t.get("count"),
                }
                for t in taglinks
            ]

        # Combos preview (panels.combocounts)
        combocounts = panels.get("combocounts", [])
        if combocounts:
            result["combos_preview"] = [
                {
                    "cards": c.get("value"),
                    "description": c.get("alt"),
                }
                for c in combocounts[:5]
            ]

        return result

    async def get_themes(self, name: str) -> list[dict]:
        """Get available themes for a commander."""
        data = await self.get_commander(name)
        return data.get("themes", [])

    async def get_combos(self, name: str) -> list[dict]:
        """Get detailed combo data for a commander."""
        slug = to_slug(name)
        data = await self._fetch(f"combos/{slug}")

        combos = []
        container = data.get("container", data)
        json_dict = container.get("json_dict", container)
        for cl in json_dict.get("cardlists", []):
            combo_info = cl.get("combo", {})
            combos.append({
                "cards": [cv.get("name") for cv in cl.get("cardviews", [])],
                "results": combo_info.get("results", []),
                "deck_count": combo_info.get("count"),
                "percentage": combo_info.get("percentage"),
                "bracket_vote": (combo_info.get("comboVote") or {}).get("bracket"),
            })
        return combos

    async def search_commanders(self, color_filter: str) -> list[dict]:
        """Browse commanders by color identity."""
        if color_filter not in VALID_COLOR_FILTERS:
            raise EDHRecError(
                f"Invalid color filter '{color_filter}'. "
                f"Valid options: {', '.join(VALID_COLOR_FILTERS)}"
            )
        data = await self._fetch(f"commanders/{color_filter}")

        container = data.get("container", data)
        json_dict = container.get("json_dict", container)
        commanders = []
        for cl in json_dict.get("cardlists", []):
            for cv in cl.get("cardviews", []):
                commanders.append({
                    "name": cv.get("name"),
                    "num_decks": cv.get("num_decks"),
                    "inclusion": cv.get("inclusion"),
                    "slug": cv.get("sanitized", to_slug(cv.get("name", ""))),
                })
        return commanders

    async def get_top_cards(self, period: str = "year") -> list[dict]:
        """Get top cards by time period."""
        valid_periods = ["year", "month", "week"]
        if period not in valid_periods:
            raise EDHRecError(f"Invalid period '{period}'. Valid: {', '.join(valid_periods)}")

        data = await self._fetch(f"top/{period}")

        container = data.get("container", data)
        json_dict = container.get("json_dict", container)
        cards = []
        for cl in json_dict.get("cardlists", []):
            for cv in cl.get("cardviews", []):
                cards.append({
                    "name": cv.get("name"),
                    "num_decks": cv.get("num_decks"),
                    "potential_decks": cv.get("potential_decks"),
                    "inclusion": cv.get("inclusion"),
                    "label": cv.get("label"),
                })
        return cards

    async def get_average_deck(self, name: str) -> dict:
        """Get the average decklist for a commander."""
        slug = to_slug(name)
        data = await self._fetch(f"average-decks/{slug}")

        container = data.get("container", data)
        json_dict = container.get("json_dict", container)

        result = {
            "name": json_dict.get("header", name),
            "card_lists": self._extract_cardlists(data),
        }
        return result

    async def get_card(self, name: str) -> dict:
        """Get card info from EDHRec (inclusion stats, top commanders, prices)."""
        slug = to_slug(name)
        data = await self._fetch(f"cards/{slug}")

        container = data.get("container", data)
        json_dict = container.get("json_dict", container)
        cardlists = json_dict.get("cardlists", [])

        # The card's own data is in json_dict.card
        card_obj = json_dict.get("card", {})
        card_info = {
            "name": card_obj.get("name") or data.get("header", name).replace(" (Card)", ""),
            "inclusion": card_obj.get("inclusion"),
            "num_decks": card_obj.get("num_decks"),
            "potential_decks": card_obj.get("potential_decks"),
            "prices": card_obj.get("prices"),
            "primary_type": card_obj.get("primary_type"),
            "rarity": card_obj.get("rarity"),
            "salt": card_obj.get("salt"),
        }

        # Top commanders that use this card
        top_commanders = []
        for cl in cardlists:
            if "commander" in cl.get("header", "").lower():
                for cv in cl.get("cardviews", []):
                    top_commanders.append({
                        "name": cv.get("name"),
                        "inclusion": cv.get("inclusion"),
                        "label": cv.get("label"),
                        "num_decks": cv.get("num_decks"),
                        "potential_decks": cv.get("potential_decks"),
                        "slug": cv.get("sanitized", to_slug(cv.get("name", ""))),
                    })
                break

        # Similar cards (at top level, has prices)
        similar = data.get("similar", [])
        similar_cards = [
            {
                "name": s.get("name"),
                "prices": s.get("prices"),
                "primary_type": s.get("primary_type"),
            }
            for s in similar[:10]
        ]

        return {
            **card_info,
            "top_commanders": top_commanders[:10],
            "similar_cards": similar_cards,
        }
