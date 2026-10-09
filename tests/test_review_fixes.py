"""Regression tests for the whole-project review fixes (v0.7.0)."""

import pytest

from mtg_commander_mcp import collection as col
from mtg_commander_mcp import server
from mtg_commander_mcp.clients.archidekt import ArchidektClient
from mtg_commander_mcp.clients.spellbook import SpellbookClient, SpellbookError


def archidekt_payload():
    def entry(eid, name, cats, qty=1):
        return {
            "id": eid, "categories": cats, "quantity": qty,
            "card": {"oracleCard": {"name": name, "cmc": 1, "typeLine": "Artifact"},
                     "edition": {"editioncode": "c20"}, "collectorNumber": str(eid),
                     "uid": f"s{eid}"},
        }
    return {
        "name": "D",
        "categories": [
            {"name": "Maybeboard", "includedInDeck": False},
            {"name": "Ramp", "includedInDeck": True},
        ],
        "cards": [
            entry(1, "Sol Ring", ["Ramp", "Artifacts"]),       # tagged twice
            entry(2, "Forest", ["Land"], qty=20),
            entry(3, "Forest", ["Land"], qty=15),              # 2nd printing
            entry(4, "Mana Crypt", ["Maybeboard"]),
            entry(5, "Atraxa", ["Commander"]),
        ],
    }


def test_mainboard_cards_dedupes_tags_and_skips_maybeboard():
    deck = ArchidektClient()._parse_deck(archidekt_payload())
    cards = col.mainboard_cards(deck)
    names = sorted((c["name"], c["quantity"]) for c in cards)
    assert names == [("Atraxa", 1), ("Forest", 15), ("Forest", 20), ("Sol Ring", 1)]
    assert next(c for c in cards if c["name"] == "Atraxa")["board"] == "Commander"


def test_mainboard_cards_skips_moxfield_sideboard():
    deck = {"categories": {"Mainboard": [{"name": "A"}], "Sideboard": [{"name": "B"}]},
            "excluded_categories": ["Sideboard", "Companions"]}
    assert [c["name"] for c in col.mainboard_cards(deck)] == ["A"]


class GC:
    async def get_game_changers(self):
        return {"rhystic study", "cyclonic rift"}


class SB:
    def __init__(self):
        self.seen = None

    async def find_deck_combos(self, commanders, cards):
        self.seen = (commanders, cards)
        return {"included": [], "one_card_away": []}


async def test_bracket_dedupes_names(monkeypatch):
    sb = SB()
    monkeypatch.setattr(server, "scryfall", GC())
    monkeypatch.setattr(server, "spellbook", sb)
    r = await server._bracket_report(
        ["Cmdr"], ["Rhystic Study", "rhystic study", "Cmdr", "Island"]
    )
    assert r["game_changers"]["count"] == 1
    assert r["minimum_bracket"] == 3
    assert sb.seen == (["Cmdr"], ["Rhystic Study", "Island"])


@pytest.mark.parametrize("card,ok", [
    ({"is_legendary": True, "is_creature": True, "type_line": "Legendary Creature"}, True),
    ({"is_legendary": True, "type_line": "Legendary Enchantment — Background"}, True),
    ({"is_legendary": True, "type_line": "Legendary Artifact — Vehicle", "power": "5"}, True),
    ({"is_legendary": True, "type_line": "Legendary Planeswalker",
      "oracle_text": "Teferi can be your commander."}, True),
    ({"is_legendary": True, "type_line": "Legendary Artifact"}, False),
    ({"is_legendary": False, "is_creature": True, "type_line": "Creature"}, False),
])
def test_can_be_commander(card, ok):
    assert server._can_be_commander(card) is ok


def test_edhrec_price_parses_both_shapes():
    assert server._edhrec_price({"cardkingdom": {"price": "$1,200.50"}}) == 1200.5
    assert server._edhrec_price({"cardkingdom": None, "tcgplayer": 3}) == 3.0
    assert server._edhrec_price(None) is None


@pytest.mark.parametrize("ci", ["ub card:x", "u b", "xyz"])
async def test_spellbook_rejects_bad_color_identity(ci):
    with pytest.raises(SpellbookError):
        await SpellbookClient().search_combos(["A"], ci)


async def test_spellbook_rejects_quote_in_card_name():
    with pytest.raises(SpellbookError):
        await SpellbookClient().search_combos(['Bad" ci:w'])


def test_archidekt_type_line_rebuilt_from_type_parts():
    f = ArchidektClient._type_line
    assert f({"superTypes": ["Legendary"], "types": ["Creature"], "subTypes": ["Elf"]}) \
        == "Legendary Creature — Elf"
    assert f({"types": ["Land"], "subTypes": []}) == "Land"
    assert f({"typeLine": "Instant"}) == "Instant"


# --- second-round fixes ----------------------------------------------------


def test_deck_card_counts_uses_mainboard_and_sums_same_printing():
    cards = [
        {"entry_id": 1, "name": "Forest", "set_code": "c20", "collector_number": "9",
         "quantity": 10},
        {"entry_id": 2, "name": "Forest", "set_code": "c20", "collector_number": "9",
         "quantity": 5},  # foil copy, separate entry
        {"entry_id": 3, "name": "Sol Ring", "set_code": "c20", "collector_number": "1",
         "quantity": 1},
    ]
    deck = {
        "categories": {
            "Land": [cards[0], cards[1]],
            "Ramp": [cards[2]], "Artifacts": [cards[2]],
            "Maybeboard": [{"entry_id": 4, "name": "Mana Crypt", "quantity": 1}],
        },
        "excluded_categories": ["Maybeboard"],
    }
    counts = col.deck_card_counts(deck)
    assert counts[("Forest", "c20", "9")]["quantity"] == 15
    assert counts[("Sol Ring", "c20", "1")]["quantity"] == 1
    assert not any(k[0] == "Mana Crypt" for k in counts)


class BoomSpellbook:
    async def find_deck_combos(self, commanders, cards):
        raise KeyError("shape changed")


async def test_bracket_survives_unexpected_upstream_errors(monkeypatch):
    class BoomScryfall:
        async def get_game_changers(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "scryfall", BoomScryfall())
    monkeypatch.setattr(server, "spellbook", BoomSpellbook())
    r = await server._bracket_report([], ["A"])
    assert "error" in r["game_changers"] and "error" in r["combos"]
    assert r["minimum_bracket"] == 1


async def test_bracket_uses_deck_site_flags(monkeypatch):
    monkeypatch.setattr(server, "scryfall", GC())
    monkeypatch.setattr(server, "spellbook", SB())
    flags = {"mass_land_denial": ["Armageddon"], "extra_turns": ["A", "B"],
             "game_changer": []}
    r = await server._bracket_report([], ["Armageddon"], flags=flags)
    assert r["minimum_bracket"] == 4 and "Armageddon" in r["reasons"][0]
    flags = {"mass_land_denial": [], "extra_turns": ["A", "B", "C"], "game_changer": []}
    assert (await server._bracket_report([], ["A"], flags=flags))["minimum_bracket"] == 4


async def test_bracket_falls_back_to_site_game_changer_flag(monkeypatch):
    class Down:
        async def get_game_changers(self):
            raise server.ScryfallError("down")

    monkeypatch.setattr(server, "scryfall", Down())
    monkeypatch.setattr(server, "spellbook", SB())
    flags = {"mass_land_denial": [], "extra_turns": [], "game_changer": ["Rhystic Study"]}
    r = await server._bracket_report([], ["Rhystic Study"], flags=flags)
    assert r["game_changers"]["source"] == "deck-site flags"
    assert r["minimum_bracket"] == 3


def test_card_flags_none_without_site_support():
    assert server._card_flags([{"name": "A"}]) is None
    f = server._card_flags([{"name": "A", "mass_land_denial": True, "extra_turns": False,
                             "game_changer": False}])
    assert f == {"mass_land_denial": ["A"], "extra_turns": [], "game_changer": []}


def test_archidekt_parses_rule_flags():
    p = archidekt_payload()
    p["cards"][0]["card"]["oracleCard"]["massLandDenial"] = True
    deck = ArchidektClient()._parse_deck(p)
    sol = next(c for c in col.mainboard_cards(deck) if c["name"] == "Sol Ring")
    assert sol["mass_land_denial"] is True and sol["extra_turns"] is False


async def test_analyze_deck_avg_cmc_uses_uncapped_values(monkeypatch):
    deck = {"name": "D", "categories": {"Commander": [
        {"name": "C", "quantity": 1, "cmc": 2, "type_line": "Creature"}],
        "Main": [{"name": "Big", "quantity": 1, "cmc": 15, "type_line": "Creature"}]}}

    async def fake_import(url):
        return deck

    class E:
        async def get_commander(self, name):
            return {"card_lists": []}

    monkeypatch.setattr(server, "_import_deck", fake_import)
    monkeypatch.setattr(server, "edhrec", E())
    monkeypatch.setattr(server, "scryfall", GC())
    monkeypatch.setattr(server, "spellbook", SB())
    r = await server.analyze_deck("https://archidekt.com/decks/1")
    assert r["avg_cmc"] == 8.5  # (2 + 15) / 2, not (2 + 7) / 2
    assert r["mana_curve"][7] == 1


async def test_price_deck_sums_separate_entries_and_skips_maybeboard(monkeypatch):
    deck = ArchidektClient()._parse_deck(archidekt_payload())

    async def fake_import(url):
        return deck

    class S:
        async def get_collection(self, names):
            assert "Mana Crypt" not in names
            return {n: {"prices": {"usd": "1.00"}} for n in names}, []

    class E:
        async def get_cards_concurrent(self, names):
            return {}

    monkeypatch.setattr(server, "_import_deck", fake_import)
    monkeypatch.setattr(server, "scryfall", S())
    monkeypatch.setattr(server, "edhrec", E())
    r = await server.price_deck("https://archidekt.com/decks/1")
    forest = next(c for c in r["card_prices"] if c["name"] == "Forest")
    assert forest["quantity"] == 35 and forest["tcgplayer_price"] == 35.0
    assert r["total_cards"] == 1 + 35 + 1  # Sol Ring + Forests + Atraxa


async def test_build_deck_keeps_cheapest_lands_when_trimming(monkeypatch):
    lands = [{"name": f"L{i}", "synergy": 0,
              "prices": {"cardkingdom": {"price": 9.0 - i * 0.2}}} for i in range(40)]

    class S:
        async def get_card(self, name):
            return {"name": "Cmdr", "is_legendary": True, "is_creature": True,
                    "type_line": "Legendary Creature", "color_identity": ["G"]}

        async def get_game_changers(self):
            return set()

    class E:
        async def get_average_deck(self, name):
            return {"card_lists": [{"category": "Lands", "cards": lands}]}

        async def get_commander(self, name, theme=None):
            return {"card_lists": []}

    monkeypatch.setattr(server, "scryfall", S())
    monkeypatch.setattr(server, "edhrec", E())
    monkeypatch.setattr(server, "spellbook", SB())
    r = await server.build_deck("Cmdr", budget="modest")
    kept = {c["name"] for c in r["deck"]["Lands"]}
    assert len(kept) == 36
    # L0-L3 are the priciest; the old code kept EDHRec order and dropped L36-L39.
    assert not kept & {"L0", "L1", "L2", "L3"}
    assert {"L36", "L37", "L38", "L39"} <= kept


async def test_bracket_handles_cancelled_child_result(monkeypatch):
    import asyncio

    class Cancelled:
        async def get_game_changers(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(server, "scryfall", Cancelled())
    monkeypatch.setattr(server, "spellbook", SB())
    r = await server._bracket_report([], ["A"])
    assert "error" in r["game_changers"]
    assert r["minimum_bracket"] == 1
