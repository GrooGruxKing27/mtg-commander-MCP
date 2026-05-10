# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
