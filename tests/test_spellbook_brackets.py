"""Tests for the Spellbook client, Game Changers lookup and bracket report.
All HTTP is routed through httpx.MockTransport — no network."""

import httpx
import pytest

from mtg_commander_mcp import server
from mtg_commander_mcp.clients.scryfall import ScryfallClient
from mtg_commander_mcp.clients.spellbook import SpellbookClient, SpellbookError


def variant(vid, names, results=("Win the game",), pop=10):
    return {
        "id": vid,
        "uses": [{"card": {"name": n}} for n in names],
        "identity": "UB",
        "produces": [{"feature": {"name": r}} for r in results],
        "description": "steps",
        "bracketTag": "R",
        "manaNeeded": "{U}",
        "popularity": pop,
    }


def install(client, handler):
    def _get_client():
        if client._client is None:
            client._client = httpx.AsyncClient(
                base_url=client.BASE_URL,
                transport=httpx.MockTransport(handler),
                timeout=1.0,
            )
        return client._client

    client._get_client = _get_client
    if hasattr(client, "REQUEST_DELAY"):
        client.REQUEST_DELAY = 0


async def test_search_combos_builds_query_and_slims():
    seen = {}

    def handler(req):
        seen["q"] = req.url.params["q"]
        return httpx.Response(200, json={"results": [variant("1-2", ["A", "B"])]})

    sb = SpellbookClient()
    install(sb, handler)
    out = await sb.search_combos(["A", "B"], "UB", limit=5)
    assert seen["q"] == 'card:"A" card:"B" ci:ub'
    assert out[0]["cards"] == ["A", "B"]
    assert out[0]["url"].endswith("/combo/1-2")


async def test_search_combos_requires_input():
    with pytest.raises(SpellbookError):
        await SpellbookClient().search_combos()


async def test_find_deck_combos_splits_included_and_near():
    def handler(req):
        return httpx.Response(200, json={"results": {
            "included": [variant("a", ["X", "Y"])],
            "almostIncluded": [
                variant("b", ["X", "Z"], pop=5),
                variant("c", ["X", "Q", "R"], pop=9),  # two missing -> dropped
            ],
        }})

    sb = SpellbookClient()
    install(sb, handler)
    out = await sb.find_deck_combos(["Cmdr"], ["X", "Y"])
    assert [c["id"] for c in out["included"]] == ["a"]
    assert [c["id"] for c in out["one_card_away"]] == ["b"]
    assert out["one_card_away"][0]["missing_cards"] == ["Z"]


async def test_spellbook_http_error_becomes_spellbook_error():
    sb = SpellbookClient()
    install(sb, lambda req: httpx.Response(500))
    with pytest.raises(SpellbookError):
        await sb.search_combos(["A"])


async def test_game_changers_paginates_and_adds_front_face():
    def handler(req):
        if "page=2" in str(req.url):
            return httpx.Response(200, json={"data": [{"name": "Sol Ring"}]})
        return httpx.Response(200, json={
            "data": [{"name": "Fire // Ice"}],
            "has_more": True,
            "next_page": "https://api.scryfall.com/cards/search?q=is%3Agamechanger&page=2",
        })

    sc = ScryfallClient()
    install(sc, handler)
    assert await sc.get_game_changers() == {"fire // ice", "fire", "sol ring"}


class FakeScryfall:
    def __init__(self, gcs=None, err=None):
        self.gcs, self.err = gcs, err

    async def get_game_changers(self):
        if self.err:
            raise server.ScryfallError(self.err)
        return self.gcs


class FakeSpellbook:
    def __init__(self, included=(), err=None):
        self.included, self.err = list(included), err

    async def find_deck_combos(self, commanders, cards):
        if self.err:
            raise SpellbookError(self.err)
        return {"included": self.included, "one_card_away": []}


def combo(cards, results):
    return {"cards": cards, "results": results}


async def test_bracket_floor_scales_with_game_changers(monkeypatch):
    monkeypatch.setattr(server, "spellbook", FakeSpellbook())
    gcs = {"a", "b", "c", "d"}

    monkeypatch.setattr(server, "scryfall", FakeScryfall(gcs))
    assert (await server._bracket_report(["Cmdr"], ["x"]))["minimum_bracket"] == 1
    assert (await server._bracket_report(["Cmdr"], ["A"]))["minimum_bracket"] == 3
    r = await server._bracket_report(["Cmdr"], ["A", "B", "C", "D"])
    assert r["minimum_bracket"] == 4 and r["game_changers"]["count"] == 4


async def test_bracket_short_winning_combo_forces_four(monkeypatch):
    monkeypatch.setattr(server, "scryfall", FakeScryfall(set()))
    monkeypatch.setattr(
        server, "spellbook",
        FakeSpellbook([combo(["A", "B"], ["Win the game"]),
                       combo(["A", "B", "C", "D"], ["Win the game"])]),
    )
    r = await server._bracket_report([], ["A", "B"])
    assert r["combos"]["short_game_winning"] == 1
    assert r["minimum_bracket"] == 4


async def test_bracket_degrades_when_upstreams_fail(monkeypatch):
    monkeypatch.setattr(server, "scryfall", FakeScryfall(err="down"))
    monkeypatch.setattr(server, "spellbook", FakeSpellbook(err="down"))
    r = await server._bracket_report([], ["A"])
    assert "error" in r["game_changers"] and "error" in r["combos"]
    assert r["minimum_bracket"] == 1
