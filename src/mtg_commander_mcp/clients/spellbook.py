import asyncio
import logging
import re
from contextlib import asynccontextmanager

import httpx

from mtg_commander_mcp import __version__
from mtg_commander_mcp.utils import Cache, discard_async_client

logger = logging.getLogger(__name__)


class SpellbookError(Exception):
    pass


class SpellbookClient:
    """Commander Spellbook (backend.commanderspellbook.com) combo lookups."""

    BASE_URL = "https://backend.commanderspellbook.com"
    HEADERS = {
        "User-Agent": f"MTGCommanderMCP/{__version__}",
        "Accept": "application/json",
    }
    # Hard ceiling on one logical call; same rationale as ScryfallClient.
    TOTAL_DEADLINE = 40.0

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache = Cache(ttl=1800)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers=self.HEADERS,
                timeout=20.0,
                limits=httpx.Limits(
                    max_connections=5,
                    max_keepalive_connections=2,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    def _reset_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            logger.warning("Resetting Commander Spellbook HTTP client")
            discard_async_client(client)

    @asynccontextmanager
    async def _deadline(self):
        try:
            async with asyncio.timeout(self.TOTAL_DEADLINE):
                yield
        except TimeoutError as e:
            self._reset_client()
            raise SpellbookError(
                f"Commander Spellbook did not answer within {self.TOTAL_DEADLINE:.0f}s "
                "— retry the call"
            ) from e
        except asyncio.CancelledError:
            self._reset_client()
            raise

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with self._deadline():
            try:
                resp = await self._get_client().request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise SpellbookError(
                    f"Commander Spellbook API error: {e.response.status_code}"
                ) from e
            except httpx.TimeoutException as e:
                self._reset_client()
                raise SpellbookError("Commander Spellbook request timed out") from e
            except httpx.TransportError as e:
                self._reset_client()
                raise SpellbookError(
                    f"Commander Spellbook connection error: {type(e).__name__}"
                ) from e
            except ValueError as e:
                raise SpellbookError("Commander Spellbook returned invalid JSON") from e

    @staticmethod
    def _slim(variant: dict, deck_names: set[str] | None = None) -> dict:
        cards = [u["card"]["name"] for u in variant.get("uses", [])]
        out = {
            "id": variant.get("id"),
            "cards": cards,
            "color_identity": variant.get("identity"),
            "results": [p["feature"]["name"] for p in variant.get("produces", [])],
            "description": variant.get("description"),
            "bracket_tag": variant.get("bracketTag"),
            "mana_needed": variant.get("manaNeeded"),
            "popularity": variant.get("popularity"),
            "url": f"https://commanderspellbook.com/combo/{variant.get('id')}",
        }
        if deck_names is not None:
            out["missing_cards"] = [c for c in cards if c.lower() not in deck_names]
        return out

    async def search_combos(
        self,
        cards: list[str] | None = None,
        color_identity: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Combos containing ALL of ``cards`` and fitting ``color_identity``
        (letters like "ug"; combos within that identity), most popular first."""
        for c in cards or []:
            if '"' in c or not c.strip():
                raise SpellbookError(f"Invalid card name: {c!r}")
        terms = [f'card:"{c.strip()}"' for c in (cards or [])]
        if color_identity:
            ci = color_identity.strip().lower()
            if not re.fullmatch(r"[wubrgc]+", ci):
                raise SpellbookError(
                    f"Invalid color_identity {color_identity!r}: use letters from WUBRGC"
                )
            terms.append(f"ci:{ci}")
        if not terms:
            raise SpellbookError("Provide at least one card or a color identity")
        limit = max(1, min(limit, 50))
        params = {"q": " ".join(terms), "limit": limit, "ordering": "-popularity"}
        key = f"search:{params}"
        cached = self._cache.get(key)
        if cached is None:
            data = await self._request("GET", "/variants/", params=params)
            cached = [self._slim(v) for v in data.get("results", [])[:limit]]
            self._cache.set(key, cached)
        return cached

    async def find_deck_combos(
        self, commanders: list[str], cards: list[str], near_limit: int = 10
    ) -> dict:
        """Combos fully present in a decklist, plus ones one card away."""
        key = f"deck:{sorted(commanders)}:{sorted(cards)}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        body = {
            "commanders": [{"card": c, "quantity": 1} for c in commanders],
            "main": [{"card": c, "quantity": 1} for c in cards],
        }
        data = await self._request("POST", "/find-my-combos", json=body)
        res = data.get("results", {})
        deck_names = {c.lower() for c in commanders + cards}
        included = [self._slim(v, deck_names) for v in res.get("included", [])]
        slimmed = (self._slim(v, deck_names) for v in res.get("almostIncluded", []))
        near = [c for c in slimmed if len(c["missing_cards"]) == 1]
        near.sort(key=lambda c: c.get("popularity") or 0, reverse=True)
        out = {"included": included, "one_card_away": near[:near_limit]}
        self._cache.set(key, out)
        return out
