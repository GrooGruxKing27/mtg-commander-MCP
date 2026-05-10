# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.6.0] — 2026-05-10 — Per-marketplace buy-list view as primary

### Added
- **`buy_list_by_marketplace`** is now populated whenever the MTGJSON
  cache is fresh (previously: only when `cheapest_printing=True`).
  Shape is **dict keyed by marketplace name** —
  `{tcgplayer: {total_usd, cards_priced, cards_unavailable,
  cards_unavailable_count}, cardkingdom: {...}, manapool: {...}}`.
  Each entry's `total_usd` is what the whole buy list would cost from
  that single shop alone (using the shop's own cheapest English-paper
  printing); `cards_unavailable` lists what the shop doesn't carry.
  Lets the user pick a single-seller order with a clear "missing N
  cards" caveat. **This is the primary buy-decision view.**
- **`buy_list_optimal_split`** — new field. Per-card cheapest USD
  marketplace selection across TCGplayer / Card Kingdom / Manapool,
  with `savings_vs_cheapest_single_marketplace` so the caller sees
  the cross-shop savings vs. the cheapest single-shop total. Carries
  `marketplaces_used`, `marketplace_card_counts`, and the per-card
  breakdown. Floor on what the buy list *could* cost, with an explicit
  docstring caveat that per-card optimization ignores shipping cost
  and seller fragmentation.

### Changed
- **`buy_list_by_marketplace` shape is breaking-changed from v0.5.0.**
  v0.5.0 returned a list `[{marketplace, subtotal_usd, item_count,
  items}]` only in cheapest-printing mode, grouping cards by their
  winning shop. v0.6.0 returns a dict keyed by marketplace where each
  entry covers the *whole* buy list (not just the cards that won at
  that shop). The cards-by-winning-shop view moved to
  `buy_list_optimal_split.marketplace_card_counts` +
  `buy_list_optimal_split.cards` (each carrying a `marketplace`).
- **`pull_and_buy_lists` docstring** now leads with the
  per-marketplace view, demotes `buy_list_set_density` to "useful for
  binder/box traversal when picking from a single-seller bundle, less
  useful for deciding *which seller* to order from."
- **`_price_buy_list_cheapest`** rebuilt around the shared
  `_build_marketplace_views` helper so per-card and per-marketplace
  logic isn't duplicated across the fast/cheapest pricing paths.

### Notes
- Cardmarket (EUR) and Cardhoarder (MTGO digital tix) are still
  intentionally omitted — mixing currencies/formats in a single
  USD-paper buy-list optimization is misleading. Raw multi-currency
  prices remain queryable via `MTGJSONClient.lookup_price`.
- Per-marketplace data is MTGJSON-only — no Scryfall fallback (the
  Scryfall API only exposes its TCGplayer affiliate price). When the
  cache is cold/stale, the new fields are `null` and a background
  refresh is kicked off; the existing buy-list pricing falls through
  to Scryfall as before.
- Empirical motivation: on a 48-card buy list with 25 distinct sets,
  set-density grouping had a top bucket of 6 cards (and a long tail of
  14 sets at 1 card each) — i.e., the "grouping" wasn't actionably
  grouping anything for a buy decision. Per-marketplace totals are
  decision-grade ("CK is $24 cheaper than TCG but missing 1 card").

## [0.4.1] — 2026-05-10 — UX polish on `pull_and_buy_lists`

### Added
- **`pull_list_set_density`** and **`buy_list_set_density`** fields
  on `pull_and_buy_lists`. Group the pull/buy lists by `set_code`,
  sorted by descending count, then ascending set_code for stable
  ties. Cards inside each bucket are sorted by leading-integer of
  `collector_number` so a binder ordered by number gets walked
  in-order. Turns a 39-card pull list across 22 sets into an
  ordered shopping path — hit the densest physical locations
  first, minimize binder/box traversal. Empirically: top 4 sets
  on the reported Muddle workload account for 44% of the pull;
  for the buy list, 45% (21/47) of cards are from a single set
  (SOC) — a hint to consolidate the TCGplayer order with a
  SOC-heavy seller.
- **`pull_list_total_count`** and **`buy_list_total_count`** —
  convenience integer fields summing `quantity` across each list.
  Saves the caller a `sum(c["quantity"] for c in ...)`.
- **`over_commit_warnings_count`** — always-present integer count
  of cards that appear in more existing decks than you own copies
  of. Lets the caller see at-a-glance whether any portfolio-level
  conflicts exist without having to count an array.

### Changed
- **`over_commit_warnings` is now opt-in.** Default behavior
  suppresses the full list (a 60+ deck portfolio routinely shows
  40-60 entries — informational noise for the current new-deck
  decision). Pass `include_over_commit_warnings=True` to
  `pull_and_buy_lists` to surface them inline. For dedicated
  allocation analysis, use `compute_card_allocation` directly —
  that tool's whole point is the allocation report and continues
  to return `over_committed` in full.

### Notes
- Purely additive on the response object — no allocator or pricer
  changes, no new I/O.
- Cards without a `set_code` (rare, parser-edge cases) land in an
  `"unknown"` bucket rather than being dropped silently.
- **Minor breaking change**: the `over_commit_warnings` field is
  no longer present by default. Callers that relied on it must
  pass `include_over_commit_warnings=True` or switch to
  `compute_card_allocation`. The `over_commit_warnings_count`
  field is always present so the caller can branch on it.

## [0.4.0] — 2026-05-10 — MTGJSON-backed local pricing

### Added
- **MTGJSON bulk-pricing client.** New
  `clients/mtgjson.py` downloads MTGJSON's
  `AllPricesToday.sqlite` (~132 MB daily snapshot) and builds a
  local `identifiers.sqlite` from `AllIdentifiers.json.bz2`
  mapping Scryfall UUIDs → MTGJSON UUIDs. Cache lives at
  `$XDG_CACHE_HOME/mtg-commander-mcp/` (defaults to
  `~/.cache/mtg-commander-mcp/`). Lookups are local SQLite reads
  (~1ms each); buy-list pricing for a 50-card deck drops from
  ~3s (Scryfall batched) to ~50ms (MTGJSON cache-hit).
- **Multi-marketplace pricing.** MTGJSON's snapshot covers
  TCGplayer, Card Kingdom, Cardmarket, Manapool, and Cardhoarder
  natively. `pull_and_buy_lists` continues to surface TCGplayer
  USD as the headline price but selects the cheapest finish
  (nonfoil / foil / etched) automatically — relevant for
  foil-precon-cheaper-than-promo cases that v0.3.2's default-
  printing path missed.
- **`refresh_pricing_data(force=False)`** MCP tool — explicit
  cache refresh. Daily prices re-download takes ~30s; full
  first-time download (prices + identifiers) is ~60-90s.
- **`pricing_cache_status()`** MCP tool — read-only cache
  inspection (sizes, ages, freshness flags).
- **Background refresh.** When `pull_and_buy_lists` observes a
  cold/stale cache, it kicks off a background refresh
  (fire-and-forget, internally lock-guarded against stampedes)
  so the next call is fast. The triggering call falls through
  to Scryfall pricing so the user isn't blocked.
- **`source_breakdown`** field on the `pull_and_buy_lists`
  response — `{"mtgjson": n, "scryfall": m, "missing": k}` so
  the caller can see which backend priced each card.

### Changed
- **`_price_buy_list_fast`** is now two-tier: MTGJSON first for
  entries with a `scryfall_id`, Scryfall batched for the rest.
  Output schema preserved; existing callers see the same fields.

### Notes
- **No new third-party dependencies.** `bz2` and `sqlite3` are
  stdlib; HTTP uses the existing `httpx`.
- **First call after fresh install** still uses the Scryfall
  path (~3s, unchanged from v0.3.2) while the cache populates
  in the background. Subsequent calls within 24h use MTGJSON.
- **Disk footprint** ~277 MB at steady state (132 MB prices +
  145 MB identifiers SQLite). Identifiers refresh only on
  Scryfall-ID misses or explicit `refresh_pricing_data(force=True)`.
- **MTGJSON is a free community resource.** No auth, no API
  key, no rate limit. We send a polite `User-Agent`.
- **Card Kingdom natively** sets up the path for `price_deck`
  to drop its EDHRec-based CK workaround in a future release.

## [0.3.2] — 2026-05-10 — fix `pull_and_buy_lists` MCP timeout

### Fixed
- **`pull_and_buy_lists` MCP timeout for ~50+ card cold buy lists.**
  Empirical timing on the reported failing call (49-card cold buy
  list against `GrooGruxKing27`'s collection) showed
  `/cards/search?unique=prints` getting soft-throttled by Scryfall
  at sustained concurrent load — 8 of 49 calls hung at ~60s each
  regardless of whether we ran serial, Semaphore(8), or
  Semaphore(4). The 100ms client-side throttle and the `_throttle`
  lock can't avoid this; it's a server-side response-deferral
  pattern.

  Default pricing path now uses the **batched
  `/cards/collection`** endpoint (75 names per POST, fuzzy fallback
  for misses) — the same flow `price_deck` and v0.3.0's
  `_price_buy_list` used. Cold 49-card buy list drops from
  timing-out at >60s to ~3-7s. Trade-off: the price returned is
  Scryfall's *default* printing, not the actually-cheapest. Most
  cards' default is at or near the cheapest; edge-cases (foil-
  precon-cheaper-than-nonfoil-promo) are no longer caught
  automatically.

  **Cheapest-printing search is preserved as opt-in** via
  `cheapest_printing=True` on `pull_and_buy_lists`. Documented to
  be slow and may time out on cold caches; for users who care
  about the edge cases.
- **Version drift between `__init__.py` and `pyproject.toml`.**
  `pyproject.toml` was bumped to 0.3.1 in commit `1507a8f`;
  `__init__.py` was missed and stayed at 0.3.0. Both now at 0.3.2.

### Diagnostic data
Cold-cache timing on the reported workload (49-card buy list,
`https://archidekt.com/decks/22444881/muddle` ×
`GrooGruxKing27`):

| Mode | Wall-clock | Coverage | Notes |
|------|-----------|----------|-------|
| Pre-fix serial cheapest | ~125s | 100% | over MCP timeout |
| Sem(8) cheapest | ~125s | 100% | Scryfall soft-throttles 8 of 49 |
| Sem(4) cheapest | ~125s | 100% | same — concurrency isn't the bottleneck |
| Default-printing batch (this fix) | ~5s | 100% | shipping default |
| Default + warm cache | <0.5s | 100% | unchanged |

## [0.3.0] — 2026-05-10 — Delver Lens collection support

Adds personal-collection workflows on top of the existing deck tools:
import a Delver Lens CSV export, then cross-reference your collection
against every deck you've already built to see what's free to use, what
you'd need to buy for a new build, and which cards you've already
double-booked across decks.

### Added
- **`import_collection`** — parse a Delver Lens CSV export (or any
  compatible Moxfield / TCGPlayer / Deckbox / MTGstand preset).
  Accepts a local file path, http(s):// URL, or pasted CSV text.
  Auto-detects column variants via a case-insensitive alias table:
  Name / Card Name / Card, Quantity / Count / Qty, Set Code / Edition,
  Foil / Printing / Finish, Scryfall ID, etc. Resolves names via
  Scryfall — exact-match batch lookup, then per-card fuzzy fallback
  for the not-found list (same edge cases `price_deck` handles:
  apostrophes, accents, casing). Returns counts + estimated USD value
  + an `unresolved` list. With `persist=True`, writes a JSON snapshot
  to `~/.local/share/mtg-commander-mcp/collection.json` (or
  `$XDG_DATA_HOME/...`).
- **`collection_summary`** — stats on the active collection: unique
  cards, total quantity, estimated USD value, color identity
  breakdown, top sets.
- **`compute_card_allocation`** — given a list of Archidekt/Moxfield
  deck URLs (and/or an Archidekt username for auto-discovery),
  returns what's `committed` to existing decks, what's `free`, and
  what's `over_committed` (used in more decks than owned).
- **`pull_and_buy_lists`** — headline workflow tool. Given a new deck
  (URL or pasted `1 Sol Ring`-style decklist) and the user's existing
  deck set, returns a **pull list** (cards to physically pull from
  the free pool of the collection), a priced **buy list** (TCGPlayer
  USD via Scryfall), and over-commit warnings. Basic lands skipped
  by default since most Delver users don't track them.
- **`ArchidektClient.list_user_decks`** — paginated public deck
  enumeration via `/api/decks/v3/?ownerUsername=<user>&ownerexact=true`
  (verified against the live API; the obvious-looking
  `/api/decks/cards/?owner=` route returns the React-server's
  "Client Unavailable" stub). Defaults to **all formats** so a
  user's Modern / Legacy / Pauper decks count as committed cards
  alongside their Commander decks — physical cards are physical.
  Optional `format_filter` accepts human names (`"commander"`,
  `"modern"`, etc.) and translates to Archidekt's numeric
  `deckFormat` codes. Pagination follows the API's `next` cursor
  with a default cap of 20 pages (~1200 decks at the API's actual
  ~60-per-page rate, despite `pageSize=50` being requested).
  Skips `private` and `unlisted` decks (the latter is opt-in via
  `include_unlisted=True`).
- **`DelverClient`** — pure CSV parser; no network I/O beyond URL
  fetch. ~150 LOC, no new third-party deps (`csv` is stdlib).
- **`collection.py`** — quantity-aware allocation math
  (`aggregate_owned`, `aggregate_committed`, `free_pool`,
  `pull_and_buy`) + a tolerant decklist parser for pasted
  `1 Sol Ring (CMM) 423` lines.
- **`storage.py`** — atomic JSON snapshot at the XDG data dir, ~50 LOC.

### Decisions
- **No screen-scraping `mtg.delver.dev/collection`.** It's a Firebase-
  authed SPA — fetching the URL returns only a copyright stub. CSV
  export is the documented, stable integration surface.
- **`.dlens` (SQLite) backup import deferred.** Schema is community-
  reverse-engineered and shifts between Delver releases; CSV covers
  ~100% of users.
- **Single active collection** (no named/multiple collections).
- **Allocation aggregates by Scryfall-canonical name**, not by
  printing. Two `Sol Ring`s in different sets count as 2 against deck
  demand. Foil/non-foil is preserved in the parsed rows but doesn't
  affect allocation.

### Changed
- **`__version__` bumped to 0.3.0** in `__init__.py` and `pyproject.toml`.
- **Tool count: 13 → 17.** README data-sources table now lists Delver
  Lens as the 6th source.

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
