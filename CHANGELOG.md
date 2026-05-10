# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.1] — 2026-05-10 — second-pass cleanup

Addresses the seven-item punch list from the v0.2.0 re-review.

### Fixed
- **README rate-limit caveat was actively misleading** — said "wait a
  minute" as though the issue were intrinsic, even though the in-process
  throttle race that amplified it was fixed in 0.2.0. Rewrote to
  distinguish the (fixed) race from the (still-real but rarer) per-IP
  burst limit, so users know the difference.
- **`build_deck` land truncation under `no_limit`** was sorting by price
  ascending — wrong, since the user explicitly opted out of price
  filtering. Now uses EDHRec's order on `no_limit`, price-ascending only
  on budget/modest tiers.
- **`scryfall_card` docstring** said "TCGPlayer + Cardmarket" but the
  data dict surfaces Scryfall's `prices` object (usd / eur / tix). Doc
  now matches.

### Added
- **Stderr logging** at every previously-silent failure site:
  `EDHRecClient.get_cards_concurrent`, `ScryfallClient.get_card_price`'s
  prints-search fallback, `analyze_deck`'s EDHRec recommendations,
  `build_deck`'s `avg_deck` and not-a-commander suggestion paths, and
  `price_deck`'s `return_exceptions=True` fallback gather. A small
  package-level config in `__init__.py` sets up a stderr WARNING
  handler unless the embedding application has already configured
  logging — keeps Claude Desktop's MCP log useful without polluting
  callers that have their own setup.
- **`get_collection` cache.** Repeat `price_deck` on the same deck
  within ~5 minutes is now ~half the cold runtime (verified: 14.1s cold
  → 6.58s warm). Keyed on `frozenset(names)` so two decks that share
  cards still benefit.
- **Glossary parser max-size sanity check.** Already warned on too-few
  entries; now also warns on >5000 (parser over-matching after a WotC
  format change is just as bad as parser falling off).

### Changed
- **`Cache` docstring** now documents that TTL is bound to write time
  and reads do not extend lifetime — a deliberate "data freshness"
  policy that wasn't called out before.
- **`get_collection` not-found tracking** uses a set for membership
  (was O(n²) over 75-item batches; trivial scale, but matches the
  call-site's `fallback_set` pattern).
- **`__version__` bumped to 0.2.1** in both `__init__.py` and
  `pyproject.toml` (they had drifted out of sync in the v0.2.0 ship).

### Removed
- **Dead `color_pips` dict** in `analyze_deck` — was initialized
  `{"W": 0, …}` and never written or returned. Faithful pip counting
  needs `mana_cost` symbols which Archidekt's parser doesn't expose;
  going with the panel's "or delete" option.
- **Unused `limit` parameter** on `EDHRecClient._extract_cardlists` —
  no caller passed it.
- **`__import__("sys").stderr`** hack in `rules.py` — replaced with a
  proper `import sys` at the top.
- **Unused `import os`** in `rules.py`.

## [0.2.0] — 2026-05-10 — panel-review pass

Addresses the must-fix and should-fix items from a panel code review.

### Fixed
- **Scryfall throttle race.** `_last_request` was being read/sleeped/written
  without serialization, so concurrent coroutines (the EDHRec fan-out, the
  TCG fallback `gather`) all observed stale timestamps and burst N requests
  simultaneously, tripping Scryfall's per-IP 429. The check-sleep-mark
  critical section now lives inside an `asyncio.Lock`, and the README's
  "wait a minute" caveat goes away.
- **Unbounded TCG fallback fan-out.** `price_deck`'s fallback `asyncio.gather`
  fired `len(needs_tcg_fallback)` Scryfall lookups in parallel — could be
  30+ simultaneous on a deck full of edge-case prints. Now bounded by a
  `Semaphore(4)`.
- **Moxfield scraper thread-unsafety.** `cloudscraper` wraps
  `requests.Session` (not thread-safe) but was driven from a thread-pool
  executor as a shared instance — concurrent Moxfield imports could corrupt
  cookie state. Now constructs a fresh scraper per call.
- **Scryfall `not_found` ignored.** `get_collection` now returns
  `(found, not_found)` and `price_deck` routes the not-found list through
  the per-card fuzzy fallback. Picks up cards exact-match misses on
  apostrophes, accents, and casing (e.g. "Jotun Grunt" vs "Jötun Grunt").
- **`build_deck` silent under-fill.** A tight budget tier could silently
  return an 80-card deck. Now adds a `warning` field listing the shortfall
  and suggesting a wider budget tier.
- **`build_deck` budget docstring lied** — said "average per-deck" while the
  implementation is per-card. Docstring now matches the code.
- **`build_deck` land truncation was order-of-EDHRec-rec.** When the
  recommended-land pool exceeds the target count, lands are now sorted by
  price ascending before truncating, so a budget build keeps the cheap
  check-lands rather than dropping them in favor of fetchlands.
- **Rules loader blocked startup on `magic.wizards.com`.** Every load did a
  synchronous HTTP scrape against WotC's rules page to check for a new
  comprehensive-rules URL, even when the cached `rules.txt` was already
  fresh. Now writes a `checked_at` timestamp in the cache metadata and only
  re-checks the WotC page after 24h have elapsed.
- **`Cache` had no size bound.** TTL alone can't cap memory in a long-
  running daemon. Added a configurable `max_size` (default 2048) with LRU
  eviction layered on top of TTL.
- **Deprecated `asyncio.get_event_loop()` calls** in `scryfall.py`,
  `moxfield.py`, and `rules.py`. Replaced with `time.monotonic()` for the
  Scryfall throttle clock, `asyncio.get_running_loop()` for the executor
  hand-offs.

### Added
- `__version__` constant in the package, used to drive User-Agent strings
  in every HTTP client (was previously hardcoded `MTGCommanderMCP/0.1.0`
  in two places).
- `Literal` types on `edhrec_top_cards.period`,
  `edhrec_search_commanders.color_identity`, and `build_deck.budget` so
  the MCP schema enumerates the valid options instead of failing at the
  service layer.
- Glossary parser sanity check: warns on stderr if it parses fewer than
  100 entries from the comprehensive rules (early-warning that WotC's text
  format has drifted).
- `LICENSE` (MIT).
- This `CHANGELOG.md`.

### Removed
- `run.sh` wrapper. It was added as a workaround for `ModuleNotFoundError`
  under Python 3.14, but the recommended `uv run --no-editable` config in the
  README now solves that without a shell-script hop — and macOS TCC was
  blocking the script anyway when the repo lives in `~/Documents/`.

## price_deck speedup — 1dfddc8 (2026-05-10)

### Changed
- `price_deck` runtime drops from ~3 minutes to ~10–20 s on a 100-card deck.
  Implementation now uses Scryfall's batched `/cards/collection` endpoint
  (75 cards per HTTP POST) plus 8-way concurrent EDHRec lookups instead of
  one fuzzy lookup per card. Also pricing coverage improved (exact-name
  matching is more reliable than fuzzy + the old serial path partially
  failed under Scryfall rate-limit pressure).

### Added
- `ScryfallClient.get_collection(names)` — batched POST to `/cards/collection`.
- `EDHRecClient.get_cards_concurrent(names, max_concurrent=8)` — parallel
  per-card lookup with a Semaphore.

### Known issue
- The new burst can briefly trip Scryfall's per-IP rate limiter, so other
  Scryfall-backed tools called within ~60 s of `price_deck` may return
  `"Scryfall rate limit exceeded after retries"`. Wait a minute and retry.

## price_deck bug fixes — 274a804 (2026-05-10)

### Fixed
- `price_deck` crashed with `'str' object has no attribute 'get'` whenever
  the deck contained a card whose EDHRec page returned its top-level
  `similar` field as a list of card-name strings instead of a list of dicts
  (e.g. Extravagant Replication). `EDHRecClient.get_card` now normalizes
  both shapes.
- `price_deck` was double-counting cards that Archidekt assigns to multiple
  user-defined categories (e.g. a card tagged both "Creature" and "Removal"
  was priced 2×). Dedupe by name before pricing.

## Setup hardening — d1e5fd2 (2026-05-10)

### Documentation
- README: added `--no-editable` to the recommended Claude Desktop /
  Claude Code configs to sidestep editable-install `.pth` issues under
  Python 3.14, and added a Troubleshooting section covering both
  `ModuleNotFoundError: No module named 'mtg_commander_mcp'` and the
  macOS TCC `Operation not permitted` pitfall when the repo lives in
  `~/Documents/`.

## Earlier — initial release & build-system shakeout

- `6742a2c` (2026-04-22): Added `run.sh` wrapper as an earlier workaround
  for the `ModuleNotFoundError` under Python 3.14. Superseded by the
  `--no-editable` config in `d1e5fd2` and removed in this changelog's
  Unreleased section.
- `0b1680e` (2026-04-13): Switched build backend from setuptools to
  hatchling.
- `fe12709` (2026-04-08): Pinned Python `<3.14` to dodge the editable-
  install regression. Later relaxed once `--no-editable` was the
  documented setup.
- `0ed0eb5`: First README with full tool documentation.
- `3330cf6`: Added `.gitignore`, removed accidentally-tracked `.DS_Store`.
- `34c7fd0`: Initial commit — MTG Commander MCP server with 13 tools
  across EDHRec, Scryfall, Archidekt, Moxfield, and the MTG Comprehensive
  Rules.
