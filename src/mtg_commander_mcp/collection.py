"""Collection model + cross-deck allocation math (printing-aware).

A "collection" is a list of canonical Delver-style row dicts (see
``clients.delver``). Allocation is keyed by ``(name, set_code,
collector_number)`` so a slot calling for Sol Ring from ``c20/252`` is
matched to that specific printing in inventory rather than colliding
with every other Sol Ring the user owns.

Match tiers (used when a deck slot's printing is under-specified —
older Archidekt decks, pasted decklists, etc.):

  1. ``exact``  — slot's full ``(name, set, cn)`` matches an owned key
  2. ``set``    — slot has set but no CN (or owned CN differs); name
                  + set match
  3. ``name``   — fall back to any owned printing of that name

The allocator picks tier-1 first, then tier-2, then tier-3 within a
slot, preferring lower collector numbers for determinism. The
over-commit warning is name-level (aggregated across printings) since
per-printing over-commit is usually a deck-data artifact and not
actionable.

Foil/finish is intentionally NOT in the match key. If a user owns a
foil and a non-foil of the same printing, they aggregate to one row
with summed quantity; the pull list tells you "grab one Sol Ring
(c20/252)" and you choose which copy to pull.
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


PrintingKey = tuple[str, str | None, str | None]


def _key(name: str | None, set_code, collector_number) -> PrintingKey | None:
    if not name:
        return None
    s = set_code.lower() if isinstance(set_code, str) and set_code else None
    c = str(collector_number) if collector_number not in (None, "") else None
    return (name, s, c)


def aggregate_owned(rows: Iterable[dict]) -> dict[PrintingKey, dict]:
    """Group collection rows by ``(name, set_code, collector_number)``.

    Returns ``dict[key, {quantity, scryfall_id}]``. The scryfall_id
    is taken from the first row in each group that carries one (Delver
    rows usually have it; older exports may not).
    """
    inv: dict[PrintingKey, dict] = {}
    for r in rows:
        key = _key(r.get("name"), r.get("set_code"), r.get("collector_number"))
        if key is None:
            continue
        entry = inv.setdefault(key, {"quantity": 0, "scryfall_id": None})
        entry["quantity"] += int(r.get("quantity", 1))
        if not entry["scryfall_id"] and r.get("scryfall_id"):
            entry["scryfall_id"] = r["scryfall_id"]
    return inv


def deck_card_counts(deck: dict) -> dict[PrintingKey, dict]:
    """Flatten a parsed deck (Archidekt/Moxfield shape) to ``{key: info}``.

    Archidekt assigns cards to multiple user categories — the same card
    entry appears in each. We dedupe by printing key, so a card listed
    in both "Ramp" and "Mana" still counts once. A deck that genuinely
    has two different printings of the same card (rare outside basic
    lands) is preserved as two distinct keys.
    """
    seen: dict[PrintingKey, dict] = {}
    for cat_cards in deck.get("categories", {}).values():
        for card in cat_cards:
            key = _key(
                card.get("name"),
                card.get("set_code"),
                card.get("collector_number"),
            )
            if key is None or key in seen:
                continue
            seen[key] = {
                "quantity": int(card.get("quantity", 1)),
                "scryfall_id": card.get("scryfall_id"),
            }
    return seen


def aggregate_committed(decks: Iterable[dict]) -> dict[PrintingKey, dict]:
    """Sum card quantities across all decks, keyed by printing."""
    totals: dict[PrintingKey, dict] = {}
    for deck in decks:
        for key, info in deck_card_counts(deck).items():
            entry = totals.setdefault(key, {"quantity": 0, "scryfall_id": None})
            entry["quantity"] += info["quantity"]
            if not entry["scryfall_id"] and info.get("scryfall_id"):
                entry["scryfall_id"] = info["scryfall_id"]
    return totals


def free_pool(
    owned: dict[PrintingKey, dict], committed: dict[PrintingKey, dict]
) -> tuple[dict[PrintingKey, dict], list[dict]]:
    """Compute (free_pool, over_committed_list) at printing granularity.

    over_committed is reported at the (name, set, cn) level for
    actionable signal — but the *practical* over-commit users care
    about is name-level, computed separately by the allocator.
    """
    free: dict[PrintingKey, dict] = {}
    over: list[dict] = []
    all_keys = set(owned) | set(committed)
    for key in all_keys:
        o = (owned.get(key) or {}).get("quantity", 0)
        c = (committed.get(key) or {}).get("quantity", 0)
        sid = (
            (owned.get(key) or {}).get("scryfall_id")
            or (committed.get(key) or {}).get("scryfall_id")
        )
        if c > o:
            over.append(
                {
                    "name": key[0],
                    "set_code": key[1],
                    "collector_number": key[2],
                    "owned": o,
                    "committed": c,
                }
            )
        free[key] = {"quantity": max(0, o - c), "scryfall_id": sid}
    return free, over


def _name_totals(inv: dict[PrintingKey, dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for (name, _s, _c), info in inv.items():
        totals[name] = totals.get(name, 0) + int(info.get("quantity", 0))
    return totals


def pull_and_buy(
    new_deck_cards: dict[PrintingKey, dict],
    free: dict[PrintingKey, dict],
    owned: dict[PrintingKey, dict],
    committed: dict[PrintingKey, dict],
    *,
    include_basics: bool = False,
) -> dict:
    """Allocate a new deck against the free pool, printing-aware.

    Output:
      pull_list: [{name, set_code, collector_number, scryfall_id,
                   quantity, from_free_pool, match_tier}]
        rows tell the user exactly which physical printing to grab.
        match_tier is one of "exact" | "set" | "name".
      buy_list: [{name, set_code, collector_number, scryfall_id, quantity}]
        the slot's requested printing is preserved if known; pricing
        and cheapest-printing override happens upstream in
        ``_price_buy_list``.
      over_commit_warnings: [{name, requested, owned, committed_other_decks}]
        name-level. Per-printing over-commit is rarely actionable.
    """
    # Mutable copy of available quantities — we deduct as we allocate.
    remaining = {k: int(v.get("quantity", 0)) for k, v in free.items()}

    # Index free pool by name for tier-2/3 fallback. We don't filter on
    # remaining>0 here because remaining mutates during iteration.
    free_keys_by_name: dict[str, list[PrintingKey]] = {}
    for key in free:
        free_keys_by_name.setdefault(key[0], []).append(key)

    name_owned_total = _name_totals(owned)
    name_committed_total = _name_totals(committed)

    pull_list: list[dict] = []
    buy_list: list[dict] = []
    warnings_by_name: dict[str, dict] = {}

    for slot_key, slot_info in new_deck_cards.items():
        name = slot_key[0]
        want = int(slot_info.get("quantity", 0))
        if want <= 0:
            continue
        if not include_basics and name in BASIC_LAND_NAMES:
            continue

        slot_set, slot_cn = slot_key[1], slot_key[2]
        slot_scryfall_id = slot_info.get("scryfall_id")

        # Build candidates among owned printings of this name. Printing
        # is a *preference*, not a hard filter: if the deck slot
        # specifies "Sol Ring from CMM" and the user only owns the M21
        # version, we still pull from inventory — the user just gets a
        # name-tier match. Sorting puts exact printing first, then same
        # set, then any printing of the name.
        candidates: list[tuple[int, PrintingKey]] = []
        for owned_key in free_keys_by_name.get(name, []):
            if remaining.get(owned_key, 0) <= 0:
                continue
            owned_set, owned_cn = owned_key[1], owned_key[2]
            if owned_set == slot_set and owned_cn == slot_cn:
                tier = 0  # exact
            elif slot_set and owned_set == slot_set:
                tier = 1  # set match
            else:
                tier = 2  # name-only fallback
            candidates.append((tier, owned_key))

        # Sort by tier ascending, then for determinism by (set_code,
        # collector_number) ascending. Collector_number is a string
        # ("191", "1a", "326★") so lexical sort is fine.
        candidates.sort(key=lambda t: (t[0], t[1][1] or "", t[1][2] or ""))

        pulled = 0
        tier_label = ("exact", "set", "name")
        for tier, ckey in candidates:
            if pulled >= want:
                break
            avail = remaining[ckey]
            if avail <= 0:
                continue
            take = min(avail, want - pulled)
            pull_list.append(
                {
                    "name": name,
                    "set_code": ckey[1],
                    "collector_number": ckey[2],
                    "scryfall_id": (free.get(ckey) or {}).get("scryfall_id"),
                    "quantity": take,
                    "from_free_pool": True,
                    "match_tier": tier_label[tier],
                }
            )
            remaining[ckey] -= take
            pulled += take

        shortfall = want - pulled
        if shortfall > 0:
            buy_list.append(
                {
                    "name": name,
                    "set_code": slot_set,
                    "collector_number": slot_cn,
                    "scryfall_id": slot_scryfall_id,
                    "quantity": shortfall,
                }
            )

        # Name-level over-commit signal (regardless of printing).
        owned_total = name_owned_total.get(name, 0)
        committed_total = name_committed_total.get(name, 0)
        if committed_total > owned_total and name not in warnings_by_name:
            warnings_by_name[name] = {
                "name": name,
                "requested": want,
                "owned": owned_total,
                "committed_other_decks": committed_total,
            }

    pull_list.sort(
        key=lambda c: (
            c["name"],
            c.get("set_code") or "",
            c.get("collector_number") or "",
        )
    )
    buy_list.sort(key=lambda c: c["name"])
    warnings = sorted(warnings_by_name.values(), key=lambda w: w["name"])

    return {
        "pull_list": pull_list,
        "buy_list": buy_list,
        "over_commit_warnings": warnings,
    }


def parse_decklist_text(text: str) -> dict[PrintingKey, dict]:
    """Parse a pasted "1 Sol Ring"-style decklist into ``{key: info}``.

    Pasted lists don't carry printing info, so every slot becomes a
    name-only key ``(name, None, None)`` and the allocator routes them
    through tier-3 fallback. Tolerates set tags ("1 Sol Ring (CMM) 423"
    — captured if useful), ``1x`` quantity prefix, ``//``/``#`` comment
    lines, and category headers like ``Mainboard:``.
    """
    counts: dict[PrintingKey, dict] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        if line.lower().endswith(":") and " " not in line:
            continue

        qty = 1
        rest = line
        first, _, after = line.partition(" ")
        token = first.rstrip("x").rstrip("X")
        if token.isdigit():
            qty = int(token)
            rest = after.strip()

        set_code: str | None = None
        collector_number: str | None = None
        # Capture "Sol Ring (CMM) 423" → set=CMM, cn=423
        if "(" in rest and ")" in rest:
            head, _, tail = rest.partition("(")
            name_part = head.strip()
            code_part, _, after_paren = tail.partition(")")
            set_code = code_part.strip().lower() or None
            cn_part = after_paren.strip()
            if cn_part:
                collector_number = cn_part.split()[0]
            rest = name_part

        if not rest:
            continue
        key = (rest, set_code, collector_number)
        entry = counts.setdefault(key, {"quantity": 0, "scryfall_id": None})
        entry["quantity"] += qty
    return counts
