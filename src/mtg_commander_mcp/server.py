import asyncio
import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

from mtg_commander_mcp.clients.edhrec import EDHRecClient, EDHRecError, VALID_COLOR_FILTERS
from mtg_commander_mcp.clients.scryfall import ScryfallClient, ScryfallError
from mtg_commander_mcp.clients.archidekt import ArchidektClient, ArchidektError
from mtg_commander_mcp.clients.moxfield import MoxfieldClient, MoxfieldError
from mtg_commander_mcp.clients.rules import RulesClient, RulesError
from mtg_commander_mcp.clients.delver import DelverClient, DelverError
from mtg_commander_mcp import collection as col_lib
from mtg_commander_mcp import storage

mcp = FastMCP("MTG Commander")

edhrec = EDHRecClient()
scryfall = ScryfallClient()
archidekt = ArchidektClient()
moxfield = MoxfieldClient()
rules = RulesClient()
delver = DelverClient()


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
    period: Literal["year", "month", "week"] = "year",
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


ColorFilter = Literal[
    "white", "blue", "black", "red", "green", "colorless",
    "azorius", "dimir", "rakdos", "gruul", "selesnya",
    "orzhov", "izzet", "golgari", "boros", "simic",
    "esper", "grixis", "jund", "naya", "bant",
    "abzan", "jeskai", "sultai", "mardu", "temur",
    "five-color",
]


@mcp.tool()
async def edhrec_search_commanders(
    color_identity: ColorFilter,
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
    mana cost, type line, legality, Scryfall pricing (usd / eur / tix), and image.

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
            except EDHRecError as e:
                logger.warning(
                    "EDHRec recommendations unavailable for %r in analyze_deck: %s",
                    commander_name, e,
                )

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
    budget: Literal["budget", "modest", "no_limit"] = "modest",
    theme: str | None = None,
) -> dict:
    """Build a full 100-card Commander deck.

    If the card is a legendary creature, it's used as the commander.
    If not, suggests 3-5 commanders that commonly use the card.

    Budget tiers (per-card price cap, NOT deck average):
      - "budget":   each non-basic card must be < $2
      - "modest":   each non-basic card must be < $10
      - "no_limit": no per-card price filter

    The result may contain fewer than 100 cards when a tight budget excludes
    too many recommended cards; in that case the response includes a
    `warning` field listing the shortfall.

    Args:
        card_name: A card name to build around
        budget: Per-card budget tier — "budget", "modest", or "no_limit"
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
        except EDHRecError as e:
            logger.warning(
                "EDHRec lookup failed for non-commander suggestion of %r: %s",
                card_name, e,
            )
            return {
                "message": f"'{card_data['name']}' is not a valid commander and no EDHRec data found for it.",
                "hint": "Try a legendary creature instead.",
            }

    commander_name = card_data["name"]

    # Fetch average deck as baseline
    try:
        avg_deck = await edhrec.get_average_deck(commander_name)
    except EDHRecError as e:
        logger.warning(
            "EDHRec average-deck unavailable for %r in build_deck (continuing "
            "with recommendations only): %s",
            commander_name, e,
        )
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
        # Two truncation strategies depending on whether the user opted into
        # a budget filter:
        #   - budget / modest: keep the cheapest lands first (cards without
        #     price data sort last). This stops us dropping a $0.30 check-
        #     land in favor of keeping a $40 fetchland just because EDHRec
        #     happened to list the fetchland earlier.
        #   - no_limit: the user explicitly opted out of price filtering, so
        #     respect EDHRec's synergy/inclusion order and don't bury
        #     expensive-but-correct lands (fetches, original duals).
        if budget == "no_limit":
            land_cards = land_cards[:target_lands]
        else:
            def _land_price(c: dict) -> float:
                prices = c.get("prices") or {}
                for key in ("cardkingdom", "tcgplayer"):
                    val = prices.get(key)
                    if isinstance(val, dict):
                        val = val.get("price")
                    if val is not None:
                        try:
                            return float(str(val).replace("$", "").replace(",", ""))
                        except (ValueError, TypeError):
                            continue
                return float("inf")

            land_cards = sorted(land_cards, key=_land_price)[:target_lands]
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

    result = {
        "commander": commander_name,
        "budget": budget,
        "theme": theme,
        "total_cards": total_cards,
        "deck": final_deck,
    }

    # Honesty: a Commander deck is 99 + commander = 100. If we couldn't fill
    # that — typically because a tight budget filtered out too many of the
    # recommended pool — say so explicitly rather than silently returning an
    # under-filled deck. Common with `budget` on combo/expensive commanders.
    if total_cards < 100:
        shortfall = 100 - total_cards
        hint = (
            f"Try `budget=\"modest\"` or `budget=\"no_limit\"`."
            if budget != "no_limit"
            else "Try a different theme or commander."
        )
        result["warning"] = (
            f"Deck is {shortfall} card(s) short of 100. The "
            f"`budget=\"{budget}\"` per-card price cap excluded too many "
            f"recommended cards to fill out the list. {hint}"
        )

    return result


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

    # Collect unique cards. Archidekt assigns each card to one or more
    # categories, so the same card appears multiple times in deck["categories"];
    # dedupe by name to avoid double-counting price.
    seen_names: set[str] = set()
    all_cards = []
    for cards in deck.get("categories", {}).values():
        for card in cards:
            name = card.get("name")
            if name and name not in seen_names:
                seen_names.add(name)
                all_cards.append(card)

    names = [c["name"] for c in all_cards]

    # Fetch Scryfall (batched, 75 cards/POST) and EDHRec (parallel, 8-way) in
    # parallel. For ~100 cards this is 2 Scryfall requests + ~100 EDHRec
    # requests (capped at 8 concurrent) instead of 100 sequential round-trips
    # to each — drops total runtime from ~3 minutes to ~10 seconds.
    try:
        (scryfall_data, scryfall_not_found), edhrec_data = await asyncio.gather(
            scryfall.get_collection(names),
            edhrec.get_cards_concurrent(names),
        )
    except ScryfallError as e:
        return {"error": f"Scryfall lookup failed: {e}"}

    tcg_total = 0.0
    ck_total = 0.0
    tcg_available = 0
    ck_available = 0
    tcg_missing = []
    ck_missing = []
    card_prices = []

    # Scryfall sometimes returns a default printing without USD pricing (e.g.
    # promo-only) AND its exact-match collection endpoint misses cards that
    # fuzzy lookup would catch (apostrophe / accent / casing edge cases —
    # "Jotun Grunt" vs "Jötun Grunt"). Track both here, then run them through
    # get_card_price's prints-search path. Seed with the exact names Scryfall
    # told us it couldn't resolve.
    needs_tcg_fallback: list[str] = list(scryfall_not_found)
    fallback_set = set(needs_tcg_fallback)

    for card in all_cards:
        name = card["name"]
        qty = card.get("quantity", 1)

        # --- TCGPlayer price via Scryfall batch ---
        tcg_price = None
        sd = scryfall_data.get(name)
        if sd is not None:
            usd = (sd.get("prices") or {}).get("usd")
            if usd:
                tcg_price = float(usd) * qty
                tcg_total += tcg_price
                tcg_available += qty
            elif name not in fallback_set:
                needs_tcg_fallback.append(name)
                fallback_set.add(name)
        elif name not in fallback_set:
            needs_tcg_fallback.append(name)
            fallback_set.add(name)

        # --- Card Kingdom price via EDHRec ---
        ck_price = None
        ed = edhrec_data.get(name)
        if ed is not None:
            prices = ed.get("prices") or {}
            ck_data = prices.get("cardkingdom")
            ck_val = ck_data.get("price") if isinstance(ck_data, dict) else ck_data
            if ck_val is not None:
                try:
                    ck_price = float(str(ck_val).replace("$", "").replace(",", "")) * qty
                    ck_total += ck_price
                    ck_available += qty
                except (ValueError, TypeError):
                    ck_missing.append(name)
            else:
                ck_missing.append(name)
        else:
            ck_missing.append(name)

        card_prices.append({
            "name": name,
            "quantity": qty,
            "tcgplayer_price": round(tcg_price, 2) if tcg_price else None,
            "cardkingdom_price": round(ck_price, 2) if ck_price else None,
        })

    # Fallback: for cards Scryfall didn't resolve in the batch (or returned
    # without USD pricing), retry per-card via get_card_price which searches
    # alternate printings. Bounded with a semaphore so a deck with many
    # fallback cards doesn't fire 30+ Scryfall requests in lockstep — that
    # bursts past the per-IP rate limiter even with the global throttle lock.
    if needs_tcg_fallback:
        fallback_sem = asyncio.Semaphore(4)

        async def _fallback_one(n: str):
            async with fallback_sem:
                return await scryfall.get_card_price(n)

        fallback_results = await asyncio.gather(
            *(_fallback_one(n) for n in needs_tcg_fallback),
            return_exceptions=True,
        )
        qty_by_name = {c["name"]: c.get("quantity", 1) for c in all_cards}
        for name, fb in zip(needs_tcg_fallback, fallback_results):
            if isinstance(fb, Exception):
                # Log the lost exception detail so the operator can tell
                # rate-limit pressure from genuinely-missing cards when
                # `cards_missing` jumps unexpectedly on a particular run.
                logger.warning(
                    "Scryfall fuzzy fallback failed for %r in price_deck: %s",
                    name, fb,
                )
                tcg_missing.append(name)
                continue
            usd = (fb.get("prices") or {}).get("usd")
            if usd:
                qty = qty_by_name[name]
                tcg_price = float(usd) * qty
                tcg_total += tcg_price
                tcg_available += qty
                # Patch the per-card breakdown with the fallback price
                for entry in card_prices:
                    if entry["name"] == name:
                        entry["tcgplayer_price"] = round(tcg_price, 2)
                        break
            else:
                tcg_missing.append(name)

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
# Collection Tools (Delver Lens)
# ---------------------------------------------------------------------------


async def _resolve_rows(rows: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    """Run a parsed Delver CSV through Scryfall to canonicalize names
    and capture pricing data.

    Returns ``(resolved_rows, scryfall_by_name, unresolved)``.

    Resolution preference:
      1. exact-match batch lookup via /cards/collection (75 names/req)
      2. fuzzy fallback for the not_found list (apostrophes, accents,
         alternate printings — same edge cases price_deck handles)
      3. anything still unresolved goes into ``unresolved`` and is
         excluded from owned-counts.
    """
    if not rows:
        return [], {}, []

    names = list({r["name"] for r in rows if r.get("name")})
    try:
        scryfall_data, not_found = await scryfall.get_collection(names)
    except ScryfallError as e:
        logger.warning("Scryfall batch resolve failed: %s", e)
        return [], {}, [{"row": r, "reason": str(e)} for r in rows]

    # Per-card fuzzy fallback for misses. Bounded fan-out (matches
    # price_deck's pattern at server.py:752) so a heavily-misspelled
    # CSV doesn't burst Scryfall's per-IP limiter.
    if not_found:
        sem = asyncio.Semaphore(4)

        async def _one(n: str):
            async with sem:
                try:
                    return n, await scryfall.get_card_price(n)
                except ScryfallError as e:
                    return n, e

        for name, result in await asyncio.gather(*(_one(n) for n in not_found)):
            if isinstance(result, ScryfallError):
                continue
            # get_card_price returns {name, prices, purchase_uris} — close
            # enough to a Scryfall card dict for our value-summing purposes.
            scryfall_data[name] = result

    resolved: list[dict] = []
    unresolved: list[dict] = []
    for r in rows:
        name = r.get("name")
        if name in scryfall_data:
            # Use Scryfall's canonical name (handles "Jotun" -> "Jötun" etc.).
            canonical = scryfall_data[name].get("name") or name
            resolved.append({**r, "name": canonical})
        else:
            unresolved.append(r)

    return resolved, scryfall_data, unresolved


def _sum_collection_value(rows: list[dict], scryfall_data: dict) -> float:
    total = 0.0
    for r in rows:
        sd = scryfall_data.get(r["name"])
        if not sd:
            continue
        usd = (sd.get("prices") or {}).get("usd")
        if not usd:
            continue
        try:
            total += float(usd) * int(r.get("quantity", 1))
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _color_breakdown(rows: list[dict], scryfall_data: dict) -> dict[str, int]:
    """Color identity histogram by total card quantity."""
    counts: dict[str, int] = {}
    for r in rows:
        sd = scryfall_data.get(r["name"])
        if not sd:
            continue
        ci = sd.get("color_identity") or []
        key = "".join(sorted(ci)) or "C"
        counts[key] = counts.get(key, 0) + int(r.get("quantity", 1))
    return counts


def _top_sets(rows: list[dict], top_n: int = 10) -> list[dict]:
    counts: dict[str, int] = {}
    for r in rows:
        code = r.get("set_code")
        if not code:
            continue
        counts[code] = counts.get(code, 0) + int(r.get("quantity", 1))
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"set_code": code, "count": qty} for code, qty in ranked[:top_n]]


@mcp.tool()
async def import_collection(
    source: str,
    persist: bool = False,
) -> dict:
    """Import a Magic collection from a Delver Lens CSV export
    (or a compatible Moxfield / TCGPlayer / Deckbox / MTGstand preset).

    `source` accepts:
      - a local file path (e.g. ``/Users/me/Downloads/delver.csv``)
      - an http(s):// URL serving CSV
      - pasted CSV text (must include a header row)

    Column variants are auto-detected: Name/Card Name/Card,
    Quantity/Count/Qty, Set Code/Edition/Set, Foil/Printing/Finish,
    Scryfall ID, etc.

    Names are resolved via Scryfall (preferring scryfall_id when
    present; batch + fuzzy fallback otherwise). Rows Scryfall can't
    match are returned in `unresolved` and excluded from totals.

    If `persist=True`, writes the parsed collection to
    ``~/.local/share/mtg-commander-mcp/collection.json`` so the other
    collection tools can run without re-passing the source.

    Args:
        source: file path, URL, or pasted CSV text
        persist: store as the active collection on disk (default False)
    """
    try:
        rows = await delver.parse_source(source)
    except DelverError as e:
        return {"error": str(e)}

    resolved, scryfall_data, unresolved = await _resolve_rows(rows)

    total_qty = sum(int(r.get("quantity", 1)) for r in resolved)
    unique = len({r["name"] for r in resolved})
    value = _sum_collection_value(resolved, scryfall_data)

    persisted_path: str | None = None
    if persist:
        try:
            payload = {
                "version": 1,
                "rows": resolved,
                "unresolved": [
                    {"name": r.get("name"), "quantity": r.get("quantity")}
                    for r in unresolved
                ],
            }
            persisted_path = str(storage.save_collection(payload))
        except OSError as e:
            logger.warning("Failed to persist collection: %s", e)
            return {
                "error": f"Parsed OK but failed to persist: {e}",
                "unique_cards": unique,
                "total_quantity": total_qty,
            }

    return {
        "unique_cards": unique,
        "total_quantity": total_qty,
        "estimated_value_usd": value,
        "unresolved": [
            {"name": r.get("name"), "quantity": r.get("quantity")}
            for r in unresolved
        ],
        "persisted": persisted_path is not None,
        "persisted_path": persisted_path,
    }


def _load_active() -> tuple[list[dict] | None, str | None]:
    """Return (rows, error). One will be None."""
    payload = storage.load_collection()
    if payload is None:
        return None, (
            "No active collection. Call import_collection(source, persist=True) first."
        )
    rows = payload.get("rows") or []
    return rows, None


@mcp.tool()
async def collection_summary(refresh: bool = False) -> dict:
    """Stats on the persisted active collection.

    Returns unique-card and total-quantity counts, estimated USD value
    (TCGPlayer via Scryfall), color identity breakdown, and the top
    sets by card count.

    Args:
        refresh: if True, re-fetches Scryfall data instead of relying
                 on the in-process cache (useful if pricing has shifted)
    """
    rows, err = _load_active()
    if err:
        return {"error": err}

    # Re-resolve to get fresh Scryfall data (cached unless refresh=True
    # ages out the entries — there's no per-call bypass yet, so
    # `refresh` is a hint rather than a hard miss). The 5-minute TTL
    # already keeps prices reasonably fresh.
    if refresh:
        scryfall._cache._store.clear()

    _, scryfall_data, _ = await _resolve_rows(rows or [])

    return {
        "unique_cards": len({r["name"] for r in rows}),
        "total_quantity": sum(int(r.get("quantity", 1)) for r in rows),
        "estimated_value_usd": _sum_collection_value(rows, scryfall_data),
        "color_identity_breakdown": _color_breakdown(rows, scryfall_data),
        "top_sets": _top_sets(rows),
    }


async def _gather_decks(
    deck_urls: list[str] | None,
    archidekt_username: str | None,
) -> tuple[list[dict], list[dict]]:
    """Fetch every deck the user pointed at.

    Returns (decks, errors). ``errors`` is a per-URL list so a single
    bad URL doesn't kill the whole tool call — the user gets a partial
    result with a clear "this URL failed" entry.
    """
    urls: list[str] = list(deck_urls or [])

    if archidekt_username:
        try:
            user_decks = await archidekt.list_user_decks(archidekt_username)
            urls.extend(d["url"] for d in user_decks)
        except ArchidektError as e:
            logger.warning(
                "list_user_decks(%r) failed; continuing with explicit URLs: %s",
                archidekt_username, e,
            )

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique_urls = [u for u in urls if not (u in seen or seen.add(u))]

    decks: list[dict] = []
    errors: list[dict] = []

    async def _fetch_one(u: str):
        try:
            d = await _import_deck(u)
            if "error" in d:
                return u, None, d["error"]
            return u, d, None
        except (ArchidektError, MoxfieldError) as e:
            return u, None, str(e)

    results = await asyncio.gather(*(_fetch_one(u) for u in unique_urls))
    for url, deck, err in results:
        if err:
            errors.append({"url": url, "error": err})
        elif deck:
            deck["__url"] = url  # for the response payload
            decks.append(deck)
    return decks, errors


@mcp.tool()
async def compute_card_allocation(
    deck_urls: list[str],
    archidekt_username: str | None = None,
) -> dict:
    """Compute, across the active collection and the listed decks, how
    each card is allocated.

    Pulls every deck URL plus (optionally) every Commander deck owned by
    `archidekt_username`. Returns:

      - `committed`: {card_name: total_qty_in_decks}
      - `free`: {card_name: max(0, owned - committed)}
      - `over_committed`: [{name, owned, committed}] — cards used in
        more decks than you actually own
      - `decks_loaded`: [{name, url, card_count}]
      - `deck_errors`: [{url, error}] — URLs that failed to import

    Args:
        deck_urls: explicit Archidekt or Moxfield deck URLs
        archidekt_username: optional, auto-include user's Commander decks
    """
    rows, err = _load_active()
    if err:
        return {"error": err}

    owned = col_lib.aggregate_owned(rows)
    decks, errors = await _gather_decks(deck_urls, archidekt_username)
    committed = col_lib.aggregate_committed(decks)
    free, over = col_lib.free_pool(owned, committed)

    return {
        "committed": _serialize_printing_inv(committed),
        "free": _serialize_printing_inv(free, include_zero=False),
        "over_committed": over,
        "decks_loaded": [
            {
                "name": d.get("name"),
                "url": d.get("__url"),
                "card_count": d.get("card_count"),
            }
            for d in decks
        ],
        "deck_errors": errors,
    }


def _serialize_printing_inv(
    inv: dict, *, include_zero: bool = True
) -> list[dict]:
    """Flatten a ``dict[(name, set, cn), {quantity, scryfall_id}]`` to
    a sorted list of JSON-safe rows. Tuple keys aren't JSON-serializable,
    so the tools that surface these inventories convert here.
    """
    rows: list[dict] = []
    for key, info in inv.items():
        qty = int(info.get("quantity", 0))
        if not include_zero and qty <= 0:
            continue
        rows.append(
            {
                "name": key[0],
                "set_code": key[1],
                "collector_number": key[2],
                "quantity": qty,
                "scryfall_id": info.get("scryfall_id"),
            }
        )
    rows.sort(
        key=lambda r: (
            r["name"],
            r.get("set_code") or "",
            r.get("collector_number") or "",
        )
    )
    return rows


async def _price_buy_list(buy_list: list[dict]) -> dict:
    """Price a buy list using the cheapest English-paper printing per card.

    Each input entry has ``{name, quantity, [set_code,
    collector_number, scryfall_id]}`` — the optional fields describe
    what the deck originally requested. The priced output row OVERRIDES
    those with the cheapest printing's info so the user can buy the
    cheap copy, not whatever variant the deck list happened to call
    for.

    Latency-wise this is one Scryfall ``/cards/search`` per unique
    name with a 4-way semaphore + 100ms global throttle — call it
    ~250ms per name, with 429 backoff adding noise. A 100-card buy
    list completes in ~30s on a cold cache.
    """
    if not buy_list:
        return {"items": [], "total_usd": 0.0, "missing": []}

    unique_names = list({c["name"] for c in buy_list})

    # Serial lookup. The Scryfall client's global 100ms throttle already
    # forces serialization between requests; concurrent fan-out just
    # piles up 429s without buying any wall-clock — empirically ~48% of
    # cards lost prices with a semaphore of 4. Serial @ ~250ms/card
    # handles a 100-card buy list in ~25s, well under the MCP timeout.
    cheapest_by_name: dict[str, dict | None] = {}
    for name in unique_names:
        try:
            cheapest_by_name[name] = await scryfall.get_cheapest_printing(name)
        except ScryfallError as e:
            logger.warning("Cheapest-printing lookup failed for %r: %s", name, e)
            cheapest_by_name[name] = None

    total = 0.0
    items: list[dict] = []
    missing: list[str] = []
    for entry in buy_list:
        name = entry["name"]
        qty = int(entry["quantity"])
        cheapest = cheapest_by_name.get(name)
        if cheapest:
            unit = float(cheapest["usd"])
            line = round(unit * qty, 2)
            total += line
            items.append(
                {
                    "name": name,
                    "quantity": qty,
                    "set_code": cheapest["set_code"],
                    "set_name": cheapest.get("set_name"),
                    "collector_number": cheapest["collector_number"],
                    "scryfall_id": cheapest["scryfall_id"],
                    "finish": cheapest.get("finish"),
                    "unit_price_usd": round(unit, 2),
                    "line_total_usd": line,
                }
            )
        else:
            missing.append(name)
            items.append(
                {
                    "name": name,
                    "quantity": qty,
                    "set_code": entry.get("set_code"),
                    "collector_number": entry.get("collector_number"),
                    "scryfall_id": entry.get("scryfall_id"),
                    "unit_price_usd": None,
                    "line_total_usd": None,
                }
            )

    return {
        "items": items,
        "total_usd": round(total, 2),
        "missing": missing,
    }


@mcp.tool()
async def pull_and_buy_lists(
    new_deck: str,
    deck_urls: list[str] | None = None,
    archidekt_username: str | None = None,
    include_basics: bool = False,
) -> dict:
    """Cross-reference your collection × existing decks × a new deck.

    Given a new deck under construction (Archidekt URL, Moxfield URL,
    or pasted ``1 Sol Ring``-style decklist text) and the set of decks
    you've already built (passed as URLs and/or auto-discovered from
    `archidekt_username`), returns:

      - `pull_list`: cards to physically pull from your collection,
        constrained by the *free* pool — i.e., copies not already
        committed to your other decks
      - `buy_list`: cards the new deck needs that aren't in the free
        pool, with per-card and total TCGPlayer prices
      - `over_commit_warnings`: cards the new deck wants that are
        already over-allocated across existing decks (e.g., new deck
        wants 1 Sol Ring but you already use it in 3 other decks while
        owning 2 copies)
      - `deck_errors`: per-URL errors from the existing-deck imports

    Basic lands are skipped by default — most Delver users don't track
    them. Set `include_basics=True` if you do.

    Args:
        new_deck: deck URL or pasted decklist
        deck_urls: list of existing-deck URLs (Archidekt/Moxfield)
        archidekt_username: optional, auto-include user's Commander decks
        include_basics: include basic lands in the buy list (default False)
    """
    rows, err = _load_active()
    if err:
        return {"error": err}

    # Resolve "new deck" — URL or pasted decklist. The result is a
    # printing-keyed dict ``{(name, set, cn): {quantity, scryfall_id}}``;
    # see collection.py for the allocator's matching tiers.
    new_deck_meta: dict
    if new_deck.startswith(("http://", "https://")):
        try:
            parsed = await _import_deck(new_deck)
            if "error" in parsed:
                return {"error": f"new_deck: {parsed['error']}"}
        except (ArchidektError, MoxfieldError) as e:
            return {"error": f"new_deck: {e}"}
        new_deck_cards = col_lib.deck_card_counts(parsed)
        new_deck_meta = {
            "source": "url",
            "url": new_deck,
            "name": parsed.get("name"),
        }
    else:
        new_deck_cards = col_lib.parse_decklist_text(new_deck)
        if not new_deck_cards:
            return {
                "error": (
                    "new_deck is neither a URL nor a parseable decklist "
                    "(expected lines like '1 Sol Ring')"
                )
            }
        new_deck_meta = {
            "source": "text",
            "card_count": sum(int(v.get("quantity", 0)) for v in new_deck_cards.values()),
        }

    owned = col_lib.aggregate_owned(rows)
    existing_decks, deck_errors = await _gather_decks(deck_urls, archidekt_username)
    committed = col_lib.aggregate_committed(existing_decks)
    free, _over = col_lib.free_pool(owned, committed)

    allocation = col_lib.pull_and_buy(
        new_deck_cards,
        free=free,
        owned=owned,
        committed=committed,
        include_basics=include_basics,
    )

    pricing = await _price_buy_list(allocation["buy_list"])

    return {
        "new_deck": new_deck_meta,
        "decks_considered": [
            {"name": d.get("name"), "url": d.get("__url")} for d in existing_decks
        ],
        "deck_errors": deck_errors,
        "pull_list": allocation["pull_list"],
        "buy_list": pricing["items"],
        "buy_list_total_usd": pricing["total_usd"],
        "buy_list_missing_prices": pricing["missing"],
        "over_commit_warnings": allocation["over_commit_warnings"],
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
