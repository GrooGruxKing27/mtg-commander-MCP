from mcp.server.fastmcp import FastMCP

from mtg_commander_mcp.clients.edhrec import EDHRecClient, EDHRecError, VALID_COLOR_FILTERS
from mtg_commander_mcp.clients.scryfall import ScryfallClient, ScryfallError
from mtg_commander_mcp.clients.archidekt import ArchidektClient, ArchidektError
from mtg_commander_mcp.clients.moxfield import MoxfieldClient, MoxfieldError
from mtg_commander_mcp.clients.rules import RulesClient, RulesError

mcp = FastMCP("MTG Commander")

edhrec = EDHRecClient()
scryfall = ScryfallClient()
archidekt = ArchidektClient()
moxfield = MoxfieldClient()
rules = RulesClient()


# ---------------------------------------------------------------------------
# EDHRec Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def edhrec_commander_recommendations(
    commander_name: str,
    theme: str | None = None,
    limit: int = 10,
) -> dict:
    """Get recommended cards for a Commander deck from EDHRec.

    Returns cards organized by category (high synergy, top cards, creatures,
    instants, etc.) with synergy scores and inclusion rates.

    Args:
        commander_name: The commander's name (e.g. "Atraxa, Praetors' Voice")
        theme: Optional theme slug (get slugs from edhrec_commander_themes)
        limit: Max cards per category (default 10)
    """
    try:
        data = await edhrec.get_commander(commander_name, theme=theme)
        # Truncate each category
        for cat in data.get("card_lists", []):
            cat["cards"] = cat["cards"][:limit]
        return data
    except EDHRecError as e:
        return {"error": str(e)}


@mcp.tool()
async def edhrec_commander_combos(
    commander_name: str,
    limit: int = 10,
) -> dict:
    """Get popular combos for a commander from EDHRec.

    Returns combo card combinations with what they achieve, popularity
    percentage, and bracket voting data.

    Args:
        commander_name: The commander's name
        limit: Max combos to return (default 10)
    """
    try:
        combos = await edhrec.get_combos(commander_name)
        return {"combos": combos[:limit]}
    except EDHRecError as e:
        return {"error": str(e)}


@mcp.tool()
async def edhrec_commander_themes(commander_name: str) -> dict:
    """Get available themes/archetypes for a commander from EDHRec.

    Returns theme names with slugs and deck counts.
    Use the slug value with edhrec_commander_recommendations' theme parameter.

    Args:
        commander_name: The commander's name
    """
    try:
        themes = await edhrec.get_themes(commander_name)
        return {"themes": themes}
    except EDHRecError as e:
        return {"error": str(e)}


@mcp.tool()
async def edhrec_top_cards(
    period: str = "year",
    limit: int = 20,
) -> dict:
    """Get the top EDH cards by time period from EDHRec.

    Args:
        period: "year", "month", or "week"
        limit: Max cards to return (default 20)
    """
    try:
        cards = await edhrec.get_top_cards(period)
        return {"cards": cards[:limit]}
    except EDHRecError as e:
        return {"error": str(e)}


@mcp.tool()
async def edhrec_search_commanders(
    color_identity: str,
    limit: int = 25,
) -> dict:
    """Browse commanders by color identity from EDHRec.

    Args:
        color_identity: Color filter slug. Valid options: white, blue, black, red,
            green, colorless, azorius, dimir, rakdos, gruul, selesnya, orzhov,
            izzet, golgari, boros, simic, esper, grixis, jund, naya, bant,
            abzan, jeskai, sultai, mardu, temur, five-color
        limit: Max commanders to return (default 25)
    """
    try:
        commanders = await edhrec.search_commanders(color_identity)
        return {"commanders": commanders[:limit]}
    except EDHRecError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Scryfall Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def scryfall_card(card_name: str) -> dict:
    """Look up a card on Scryfall. Returns full card data including oracle text,
    mana cost, type line, legality, pricing (TCGPlayer + Cardmarket), and image.

    Args:
        card_name: The card's name (fuzzy matching supported)
    """
    try:
        return await scryfall.get_card(card_name)
    except ScryfallError as e:
        return {"error": str(e)}


@mcp.tool()
async def scryfall_search(query: str, limit: int = 20) -> dict:
    """Search for cards using Scryfall search syntax.

    Examples: "c:ug type:creature cmc<=3", "o:draw t:instant ci:dimir",
    "is:commander id:simic"

    Args:
        query: Scryfall search query
        limit: Max results (default 20)
    """
    try:
        cards = await scryfall.search(query, limit=limit)
        return {"cards": cards}
    except ScryfallError as e:
        return {"error": str(e)}


@mcp.tool()
async def scryfall_rulings(card_name: str) -> dict:
    """Get official rulings for a card from Scryfall.

    Args:
        card_name: The card's name
    """
    try:
        rulings_list = await scryfall.get_rulings(card_name)
        return {"rulings": rulings_list}
    except ScryfallError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Deck Tools
# ---------------------------------------------------------------------------


async def _import_deck(url: str) -> dict:
    """Internal helper to import a deck from Archidekt or Moxfield URL."""
    if "archidekt.com" in url:
        return await archidekt.get_deck(url)
    elif "moxfield.com" in url:
        return await moxfield.get_deck(url)
    else:
        return {"error": "Unsupported URL. Provide an Archidekt or Moxfield deck URL."}


@mcp.tool()
async def import_deck(url: str) -> dict:
    """Import a deck from an Archidekt or Moxfield URL.

    Returns the full card list grouped by category with quantities.

    Args:
        url: Full URL to the deck (e.g. https://archidekt.com/decks/12345 or
             https://www.moxfield.com/decks/abc123)
    """
    try:
        return await _import_deck(url)
    except (ArchidektError, MoxfieldError) as e:
        return {"error": str(e)}


@mcp.tool()
async def analyze_deck(url: str) -> dict:
    """Import and analyze a Commander deck from Archidekt or Moxfield.

    Returns mana curve, color distribution, card type breakdown, mana base
    suggestions, and card recommendations based on EDHRec synergy data.

    Args:
        url: Full URL to the deck
    """
    try:
        deck = await _import_deck(url)
        if "error" in deck:
            return deck

        # Collect all cards across categories
        all_cards = []
        commander_name = None
        for cat_name, cards in deck.get("categories", {}).items():
            for card in cards:
                all_cards.append({**card, "board": cat_name})
                if cat_name.lower() == "commander" and commander_name is None:
                    commander_name = card.get("name")

        # Mana curve
        mana_curve: dict[int, int] = {}
        color_pips: dict[str, int] = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}
        type_counts: dict[str, int] = {}
        land_count = 0
        nonland_count = 0

        for card in all_cards:
            qty = card.get("quantity", 1)
            cmc = card.get("cmc")
            type_line = card.get("type_line", "") or ""

            if cmc is not None and "Land" not in type_line:
                bucket = int(cmc)
                if bucket > 7:
                    bucket = 7  # 7+ bucket
                mana_curve[bucket] = mana_curve.get(bucket, 0) + qty

            # Type counting
            if "Land" in type_line:
                land_count += qty
            else:
                nonland_count += qty

            for t in ["Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker"]:
                if t in type_line:
                    type_counts[t] = type_counts.get(t, 0) + qty

        # Sort mana curve
        mana_curve_sorted = {k: mana_curve.get(k, 0) for k in range(8)}

        # Get EDHRec recommendations if we found a commander
        edhrec_suggestions = []
        if commander_name:
            try:
                rec_data = await edhrec.get_commander(commander_name)
                # Find high synergy cards not in the deck
                deck_card_names = {c.get("name", "").lower() for c in all_cards}
                for cat in rec_data.get("card_lists", []):
                    if "synergy" in cat.get("category", "").lower():
                        for card in cat.get("cards", []):
                            if card.get("name", "").lower() not in deck_card_names:
                                edhrec_suggestions.append(card)
                                if len(edhrec_suggestions) >= 10:
                                    break
                    if len(edhrec_suggestions) >= 10:
                        break
            except EDHRecError:
                pass

        # Mana base analysis
        total_cards = sum(c.get("quantity", 1) for c in all_cards)
        mana_base = {
            "land_count": land_count,
            "nonland_count": nonland_count,
            "total_cards": total_cards,
            "land_percentage": round(land_count / total_cards * 100, 1) if total_cards else 0,
            "recommendation": None,
        }
        if land_count < 33:
            mana_base["recommendation"] = f"Low land count ({land_count}). Consider adding {36 - land_count} more lands."
        elif land_count > 40:
            mana_base["recommendation"] = f"High land count ({land_count}). You may be able to cut {land_count - 37} lands."

        return {
            "deck_name": deck.get("name"),
            "commander": commander_name,
            "card_count": total_cards,
            "mana_curve": mana_curve_sorted,
            "avg_cmc": round(
                sum(k * v for k, v in mana_curve.items()) / max(sum(mana_curve.values()), 1), 2
            ),
            "type_breakdown": type_counts,
            "mana_base": mana_base,
            "edhrec_suggestions": edhrec_suggestions,
            "categories": {k: len(v) for k, v in deck.get("categories", {}).items()},
        }
    except (ArchidektError, MoxfieldError) as e:
        return {"error": str(e)}


@mcp.tool()
async def build_deck(
    card_name: str,
    budget: str = "modest",
    theme: str | None = None,
) -> dict:
    """Build a full 100-card Commander deck.

    If the card is a legendary creature, it's used as the commander.
    If not, suggests 3-5 commanders that commonly use the card.

    Budget tiers: "budget" (<$2/card avg), "modest" (<$10/card avg), "no_limit"

    Args:
        card_name: A card name to build around
        budget: Budget tier - "budget", "modest", or "no_limit"
        theme: Optional theme slug from edhrec_commander_themes
    """
    budget_limits = {"budget": 2.0, "modest": 10.0, "no_limit": float("inf")}
    price_cap = budget_limits.get(budget, 10.0)

    try:
        # Check if the card is a valid commander
        card_data = await scryfall.get_card(card_name)
    except ScryfallError as e:
        return {"error": f"Could not find card: {e}"}

    is_commander = card_data.get("is_legendary") and card_data.get("is_creature")

    if not is_commander:
        # Not a commander — suggest some
        try:
            edhrec_card = await edhrec.get_card(card_name)
            top_cmds = edhrec_card.get("top_commanders", [])[:5]
            return {
                "message": f"'{card_data['name']}' is not a valid commander (not a legendary creature). "
                           f"Here are commanders that commonly include it:",
                "suggested_commanders": top_cmds,
                "hint": "Call build_deck again with one of these commander names.",
            }
        except EDHRecError:
            return {
                "message": f"'{card_data['name']}' is not a valid commander and no EDHRec data found for it.",
                "hint": "Try a legendary creature instead.",
            }

    commander_name = card_data["name"]

    # Fetch average deck as baseline
    try:
        avg_deck = await edhrec.get_average_deck(commander_name)
    except EDHRecError:
        avg_deck = None

    # Fetch recommendations for enrichment
    try:
        recs = await edhrec.get_commander(commander_name, theme=theme)
    except EDHRecError as e:
        return {"error": f"Could not get EDHRec data for {commander_name}: {e}"}

    # Collect cards from average deck
    deck_cards: dict[str, dict] = {}  # name -> card info

    if avg_deck:
        for cat in avg_deck.get("card_lists", []):
            for card in cat.get("cards", []):
                name = card.get("name")
                if name and name.lower() != commander_name.lower():
                    deck_cards[name] = {**card, "source": "average_deck"}

    # Enrich with high synergy cards from recommendations
    for cat in recs.get("card_lists", []):
        for card in cat.get("cards", []):
            name = card.get("name")
            if name and name not in deck_cards and name.lower() != commander_name.lower():
                synergy = card.get("synergy", 0) or 0
                if synergy > 0:
                    deck_cards[name] = {**card, "source": "high_synergy"}

    # Apply budget filtering using price data
    if price_cap < float("inf"):
        filtered = {}
        for name, card in deck_cards.items():
            price = None
            prices = card.get("prices")
            if prices:
                # EDHRec prices are nested: {"cardkingdom": {"price": 2.29}, "tcgplayer": {"price": 1.50}}
                for key in ["cardkingdom", "tcgplayer"]:
                    val = prices.get(key)
                    if isinstance(val, dict):
                        val = val.get("price")
                    if val is not None:
                        try:
                            price = float(str(val).replace("$", "").replace(",", ""))
                            break
                        except (ValueError, TypeError):
                            continue
            if price is None or price <= price_cap:
                filtered[name] = card
        deck_cards = filtered

    # Build the final deck grouped by type
    grouped: dict[str, list[dict]] = {
        "Commander": [{"name": commander_name, "quantity": 1}],
        "Creatures": [],
        "Instants": [],
        "Sorceries": [],
        "Artifacts": [],
        "Enchantments": [],
        "Planeswalkers": [],
        "Lands": [],
        "Other": [],
    }

    # Build category map from both average deck and recommendations
    category_map = {}

    def _map_category(cat_header: str) -> str | None:
        h = cat_header.lower()
        if "creature" in h:
            return "Creatures"
        if "instant" in h:
            return "Instants"
        if "sorcery" in h or "sorcer" in h:
            return "Sorceries"
        if "artifact" in h or "equipment" in h:
            return "Artifacts"
        if "enchantment" in h:
            return "Enchantments"
        if "planeswalker" in h:
            return "Planeswalkers"
        if "land" in h or "basic" in h:
            return "Lands"
        return None

    # Map from average deck categories first
    if avg_deck:
        for cat in avg_deck.get("card_lists", []):
            mapped = _map_category(cat.get("category", ""))
            if mapped:
                for card in cat.get("cards", []):
                    name = card.get("name")
                    if name:
                        category_map[name] = mapped

    # Override/supplement with recommendations categories
    for cat in recs.get("card_lists", []):
        mapped = _map_category(cat.get("category", ""))
        if mapped:
            for card in cat.get("cards", []):
                name = card.get("name")
                if name:
                    category_map[name] = mapped

    for name, card in deck_cards.items():
        cat_name = category_map.get(name, "Other")
        grouped[cat_name].append({"name": name, "quantity": 1, "synergy": card.get("synergy")})

    # Trim to 99 nonland + commander = 100
    # Prioritize: keep all lands, then sort others by synergy
    land_cards = grouped["Lands"]
    nonland_cats = ["Creatures", "Instants", "Sorceries", "Artifacts", "Enchantments", "Planeswalkers", "Other"]

    # Ensure reasonable land count (35-38) by padding with basic lands if needed
    target_lands = 36
    # Count actual land slots (respecting quantity)
    land_slot_count = sum(c.get("quantity", 1) for c in land_cards)
    if land_slot_count < target_lands:
        color_to_basic = {
            "W": "Plains", "U": "Island", "B": "Swamp",
            "R": "Mountain", "G": "Forest",
        }
        ci = card_data.get("color_identity", [])
        basic_types = [color_to_basic[c] for c in ci if c in color_to_basic]
        if not basic_types:
            basic_types = ["Wastes"]
        basics_needed = target_lands - land_slot_count
        per_type = basics_needed // len(basic_types)
        remainder = basics_needed % len(basic_types)
        for i, basic in enumerate(basic_types):
            qty = per_type + (1 if i < remainder else 0)
            if qty > 0:
                # Merge with existing entry if present
                existing = next((c for c in land_cards if c["name"] == basic), None)
                if existing:
                    existing["quantity"] = existing.get("quantity", 1) + qty
                else:
                    land_cards.append({"name": basic, "quantity": qty, "synergy": None})
        grouped["Lands"] = land_cards

    land_slot_count = sum(c.get("quantity", 1) for c in land_cards)
    if land_slot_count > target_lands:
        land_cards = land_cards[:target_lands]
        grouped["Lands"] = land_cards

    land_slot_total = sum(c.get("quantity", 1) for c in grouped["Lands"])
    remaining_slots = 99 - land_slot_total

    # Sort each nonland category by synergy descending
    for cat_name in nonland_cats:
        grouped[cat_name].sort(key=lambda c: c.get("synergy") or 0, reverse=True)

    # Distribute remaining slots proportionally
    total_nonland_available = sum(len(grouped[c]) for c in nonland_cats)
    final_deck: dict[str, list[dict]] = {"Commander": grouped["Commander"], "Lands": grouped["Lands"]}
    slots_used = 0

    for cat_name in nonland_cats:
        if slots_used >= remaining_slots:
            break
        available = grouped[cat_name]
        if not available:
            continue
        # Proportional allocation with minimum of 0
        share = max(1, int(len(available) / max(total_nonland_available, 1) * remaining_slots))
        take = min(share, len(available), remaining_slots - slots_used)
        final_deck[cat_name] = available[:take]
        slots_used += take

    # Fill remaining slots from largest categories
    if slots_used < remaining_slots:
        for cat_name in nonland_cats:
            current = len(final_deck.get(cat_name, []))
            available = grouped[cat_name][current:]
            for card in available:
                if slots_used >= remaining_slots:
                    break
                final_deck.setdefault(cat_name, []).append(card)
                slots_used += 1

    total_cards = sum(
        sum(c.get("quantity", 1) for c in cards)
        for cards in final_deck.values()
    )

    return {
        "commander": commander_name,
        "budget": budget,
        "theme": theme,
        "total_cards": total_cards,
        "deck": final_deck,
    }


# ---------------------------------------------------------------------------
# Purchase Tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def price_deck(url: str) -> dict:
    """Price out a deck from Archidekt or Moxfield on TCGPlayer and Card Kingdom.

    Compares total cost and card availability between stores and recommends
    which to buy from.

    Args:
        url: Full URL to an Archidekt or Moxfield deck
    """
    try:
        deck = await _import_deck(url)
        if "error" in deck:
            return deck
    except (ArchidektError, MoxfieldError) as e:
        return {"error": str(e)}

    # Collect all unique card names
    all_cards = []
    for cards in deck.get("categories", {}).values():
        for card in cards:
            all_cards.append(card)

    tcg_total = 0.0
    ck_total = 0.0
    tcg_available = 0
    ck_available = 0
    tcg_missing = []
    ck_missing = []
    card_prices = []

    for card in all_cards:
        name = card.get("name", "")
        qty = card.get("quantity", 1)

        # Get Scryfall price (TCGPlayer)
        tcg_price = None
        try:
            price_data = await scryfall.get_card_price(name)
            prices = price_data.get("prices", {})
            usd = prices.get("usd")
            if usd:
                tcg_price = float(usd) * qty
                tcg_total += tcg_price
                tcg_available += qty
            else:
                tcg_missing.append(name)
        except ScryfallError:
            tcg_missing.append(name)

        # Get EDHRec price (Card Kingdom)
        ck_price = None
        try:
            edhrec_card = await edhrec.get_card(name)
            prices = edhrec_card.get("prices") or {}
            ck_data = prices.get("cardkingdom")
            if isinstance(ck_data, dict):
                ck_val = ck_data.get("price")
            else:
                ck_val = ck_data
            if ck_val is not None:
                try:
                    ck_price = float(str(ck_val).replace("$", "").replace(",", "")) * qty
                    ck_total += ck_price
                    ck_available += qty
                except (ValueError, TypeError):
                    ck_missing.append(name)
            else:
                ck_missing.append(name)
        except EDHRecError:
            ck_missing.append(name)

        card_prices.append({
            "name": name,
            "quantity": qty,
            "tcgplayer_price": round(tcg_price, 2) if tcg_price else None,
            "cardkingdom_price": round(ck_price, 2) if ck_price else None,
        })

    total_cards = sum(c.get("quantity", 1) for c in all_cards)

    # Recommendation
    recommendation = None
    if tcg_available > ck_available:
        recommendation = "TCGPlayer"
        reason = "better card availability"
    elif ck_available > tcg_available:
        recommendation = "Card Kingdom"
        reason = "better card availability"
    elif tcg_total < ck_total:
        recommendation = "TCGPlayer"
        reason = "lower total cost"
    elif ck_total < tcg_total:
        recommendation = "Card Kingdom"
        reason = "lower total cost"
    else:
        recommendation = "Either"
        reason = "similar pricing and availability"

    return {
        "deck_name": deck.get("name"),
        "total_cards": total_cards,
        "tcgplayer": {
            "total_price": round(tcg_total, 2),
            "cards_available": tcg_available,
            "cards_missing": len(tcg_missing),
            "missing_cards": tcg_missing[:20],
        },
        "card_kingdom": {
            "total_price": round(ck_total, 2),
            "cards_available": ck_available,
            "cards_missing": len(ck_missing),
            "missing_cards": ck_missing[:20],
        },
        "recommendation": f"{recommendation} ({reason})",
        "card_prices": card_prices,
    }


# ---------------------------------------------------------------------------
# Rules Tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def mtg_rules(query: str, limit: int = 10) -> dict:
    """Search the MTG Comprehensive Rules by keyword or rule number.

    Auto-downloads the latest rules from Wizards of the Coast on first use
    and checks for updates on each server startup.

    Args:
        query: A rule number (e.g. "702.5") or keyword (e.g. "flying",
               "state-based actions")
        limit: Max results to return (default 10)
    """
    try:
        results = await rules.search(query, limit=limit)
        if not results:
            return {"message": f"No rules found matching '{query}'", "results": []}
        return {"results": results}
    except RulesError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
