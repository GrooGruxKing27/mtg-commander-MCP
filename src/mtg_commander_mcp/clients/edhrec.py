import asyncio
import logging
import re

import httpx

from mtg_commander_mcp import __version__
from mtg_commander_mcp.utils import Cache, discard_async_client, to_slug

logger = logging.getLogger(__name__)


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
    # Bound on chained JSON-level redirects; without it two pages that
    # redirect to each other would recurse until the stack blew.
    MAX_REDIRECTS = 5

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=300)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": f"MTGCommanderMCP/{__version__}"},
                # Expire idle keep-alives before EDHRec's CDN drops them
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

        A request cancelled mid-flight or killed at the transport layer can
        leave httpx's pool wedged; dropping the client lets the next call
        self-heal instead of hanging until the server is restarted.
        """
        client, self._client = self._client, None
        if client is not None:
            logger.warning("Resetting EDHRec HTTP client (pool may be wedged)")
            discard_async_client(client)

    async def _fetch(self, path: str, _depth: int = 0) -> dict:
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
            self._reset_client()
            raise EDHRecError("EDHRec request timed out") from e
        except httpx.TransportError as e:
            self._reset_client()
            raise EDHRecError(f"EDHRec connection error: {type(e).__name__}") from e
        except asyncio.CancelledError:
            self._reset_client()
            raise

        # Handle EDHRec JSON-level redirects
        if isinstance(data, dict) and "redirect" in data:
            if _depth >= self.MAX_REDIRECTS:
                raise EDHRecError(f"EDHRec redirect loop at: {path}")
            redirect_path = data["redirect"].strip("/")
            data = await self._fetch(redirect_path, _depth + 1)
            # Cache under the original path too so the next call skips the hop.
            self._cache.set(path, data)
            return data

        self._cache.set(path, data)
        return data

    def _extract_cardlists(self, data: dict) -> list[dict]:
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
            if cards:
                categories.append({"category": header, "cards": cards})
        return categories

    async def get_commander(self, name: str, theme: str | None = None) -> dict:
        """Get commander recommendations, optionally filtered by theme."""
        slug = to_slug(name)
        if theme:
            theme = theme.strip().lower()
            # Taglink slugs from EDHRec (e.g. "+1-1-counters") pass through;
            # anything else (spaces, "/", "?", "..") is slugged so it can't
            # alter the request path.
            if not re.fullmatch(r"[a-z0-9+\-]+", theme):
                theme = to_slug(theme)
            if not theme:
                raise EDHRecError("Invalid theme")
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

        # Similar cards (at top level). EDHRec returns this as either a list
        # of strings (card names) or a list of dicts depending on the page;
        # normalize both shapes.
        similar_cards = []
        for s in data.get("similar", [])[:10]:
            if isinstance(s, dict):
                similar_cards.append({
                    "name": s.get("name"),
                    "prices": s.get("prices"),
                    "primary_type": s.get("primary_type"),
                })
            elif isinstance(s, str):
                similar_cards.append({"name": s, "prices": None, "primary_type": None})

        return {
            **card_info,
            "top_commanders": top_commanders[:10],
            "similar_cards": similar_cards,
        }

    async def get_cards_concurrent(
        self, names: list[str], max_concurrent: int = 8
    ) -> dict[str, dict]:
        """Look up many cards in parallel, returning {name: card_data}.

        Uses a semaphore to cap concurrent requests at `max_concurrent` to
        avoid hammering EDHRec. Cards that 404 or otherwise fail are absent
        from the result.
        """
        if not names:
            return {}

        sem = asyncio.Semaphore(max_concurrent)

        async def fetch_one(name: str) -> tuple[str, dict | None]:
            async with sem:
                try:
                    return name, await self.get_card(name)
                except (EDHRecError, httpx.HTTPError) as e:
                    logger.warning("EDHRec lookup failed for %r: %s", name, e)
                    return name, None

        results = await asyncio.gather(*(fetch_one(n) for n in names))
        return {name: data for name, data in results if data is not None}
