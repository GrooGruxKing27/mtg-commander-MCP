"""Collection model + cross-deck allocation math.

A "collection" is a list of canonical Delver-style row dicts (see
``clients.delver``). We store quantities by Scryfall-canonical name,
since printing-aware allocation isn't a v0.3.0 goal — most users want
"do I own a Sol Ring at all?", not "do I own the *Commander Legends*
printing specifically?".

The allocation pipeline:

  1. ``aggregate_owned``      — collection rows  → {name: qty}
  2. ``aggregate_committed``  — list of decks    → {name: qty}
  3. ``free_pool``            — owned − committed (clamped at 0)
  4. ``pull_and_buy``         — new deck × free pool → pull / buy / warn
"""

from __future__ import annotations

from typing import Iterable

# Per Scryfall, basic lands have type "Basic Land". Hard-coded names
# avoid an extra Scryfall round-trip just to identify them; the snow-
# covered, unhinged, and full-art variants all share these names.
BASIC_LAND_NAMES = frozenset(
    {
        "Plains",
        "Island",
        "Swamp",
        "Mountain",
        "Forest",
        "Wastes",
        "Snow-Covered Plains",
        "Snow-Covered Island",
        "Snow-Covered Swamp",
        "Snow-Covered Mountain",
        "Snow-Covered Forest",
        "Snow-Covered Wastes",
    }
)


def aggregate_owned(rows: Iterable[dict]) -> dict[str, int]:
    """Sum quantities per name across all collection rows."""
    totals: dict[str, int] = {}
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        totals[name] = totals.get(name, 0) + int(r.get("quantity", 1))
    return totals


def deck_card_counts(deck: dict) -> dict[str, int]:
    """Flatten a parsed deck (Archidekt/Moxfield shape) to {name: qty}.

    Archidekt assigns cards to multiple user categories — the same card
    appears in each, so we dedupe by name and take the first quantity.
    """
    seen: dict[str, int] = {}
    for cat_cards in deck.get("categories", {}).values():
        for card in cat_cards:
            name = card.get("name")
            if not name or name in seen:
                continue
            seen[name] = int(card.get("quantity", 1))
    return seen


def aggregate_committed(decks: Iterable[dict]) -> dict[str, int]:
    """Sum card quantities across all decks."""
    totals: dict[str, int] = {}
    for deck in decks:
        for name, qty in deck_card_counts(deck).items():
            totals[name] = totals.get(name, 0) + qty
    return totals


def free_pool(
    owned: dict[str, int], committed: dict[str, int]
) -> tuple[dict[str, int], list[dict]]:
    """Compute (free_pool, over_committed_list).

    free_pool[name] = max(0, owned[name] - committed[name]) for any
    name owned. over_committed lists names where committed > owned —
    typically the user shares a single copy across multiple decks.
    """
    free: dict[str, int] = {}
    over: list[dict] = []
    all_names = set(owned) | set(committed)
    for name in all_names:
        o = owned.get(name, 0)
        c = committed.get(name, 0)
        if c > o:
            over.append({"name": name, "owned": o, "committed": c})
        free[name] = max(0, o - c)
    return free, over


def pull_and_buy(
    new_deck_cards: dict[str, int],
    free: dict[str, int],
    owned: dict[str, int],
    committed: dict[str, int],
    *,
    include_basics: bool = False,
) -> dict:
    """Allocate a new deck against the free pool.

    Returns:
      pull_list: [{name, quantity, from_free_pool}]
        cards satisfied from the available collection. quantity is
        clamped to whatever's in `free`.
      buy_list: [{name, quantity}]
        unmet demand — what the user must purchase.
      over_commit_warnings: [{name, requested, owned, committed_other_decks}]
        cards the new deck wants that are already in deficit.
    """
    pull_list: list[dict] = []
    buy_list: list[dict] = []
    warnings: list[dict] = []

    for name, want in new_deck_cards.items():
        if want <= 0:
            continue
        if not include_basics and name in BASIC_LAND_NAMES:
            continue

        available = free.get(name, 0)
        pulled = min(available, want)
        shortfall = want - pulled

        if pulled > 0:
            pull_list.append(
                {"name": name, "quantity": pulled, "from_free_pool": True}
            )

        if shortfall > 0:
            buy_list.append({"name": name, "quantity": shortfall})

        # Warn whenever the user already over-allocates this card across
        # other decks — even if they happen to also own enough copies for
        # this new deck, the existing decks are already short.
        committed_elsewhere = committed.get(name, 0)
        owned_qty = owned.get(name, 0)
        if committed_elsewhere > owned_qty:
            warnings.append(
                {
                    "name": name,
                    "requested": want,
                    "owned": owned_qty,
                    "committed_other_decks": committed_elsewhere,
                }
            )

    pull_list.sort(key=lambda c: c["name"])
    buy_list.sort(key=lambda c: c["name"])
    warnings.sort(key=lambda c: c["name"])

    return {
        "pull_list": pull_list,
        "buy_list": buy_list,
        "over_commit_warnings": warnings,
    }


def parse_decklist_text(text: str) -> dict[str, int]:
    """Parse a pasted "1 Sol Ring"-style decklist into {name: qty}.

    Tolerates set tags ("1 Sol Ring (CMM) 423"), leading/trailing
    whitespace, blank lines, and `//`/`#` comment lines. Falls back to
    quantity 1 if no leading number is present.
    """
    counts: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        # Strip a sideboard/category header like "Mainboard" / "Commander"
        if line.lower().endswith(":") and " " not in line:
            continue

        qty = 1
        rest = line
        first, _, after = line.partition(" ")
        # Tolerate "1x Sol Ring" as well as "1 Sol Ring".
        token = first.rstrip("x").rstrip("X")
        if token.isdigit():
            qty = int(token)
            rest = after.strip()

        # Strip trailing set/CN annotation: "Sol Ring (CMM) 423"
        if "(" in rest:
            rest = rest.split("(")[0].strip()

        if not rest:
            continue
        counts[rest] = counts.get(rest, 0) + qty
    return counts
