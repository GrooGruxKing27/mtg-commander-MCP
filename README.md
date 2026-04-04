# MTG Commander MCP Server

A Model Context Protocol (MCP) server for Magic: The Gathering Commander/EDH. Provides 13 tools across 5 data sources for card lookup, deck building, price comparison, and rules reference.

## Data Sources

| Source | What It Provides |
|---|---|
| **EDHRec** | Commander recommendations, combos, themes, top cards, deck averages |
| **Scryfall** | Card data, oracle text, legality, TCGPlayer pricing, rulings |
| **Archidekt** | Deck import from URL |
| **Moxfield** | Deck import from URL (Cloudflare bypass via cloudscraper) |
| **MTG Comprehensive Rules** | Searchable rules and glossary (auto-downloads latest from Wizards) |

## Tools

### EDHRec
- **`edhrec_commander_recommendations`** - Top cards for a commander by category with synergy scores and inclusion rates. Supports theme filtering.
- **`edhrec_commander_combos`** - Popular combos with results, popularity percentage, and bracket data.
- **`edhrec_commander_themes`** - Available archetypes/themes with slugs and deck counts.
- **`edhrec_top_cards`** - Top EDH cards by time period (year/month/week).
- **`edhrec_search_commanders`** - Browse commanders by color identity.

### Scryfall
- **`scryfall_card`** - Full card data including oracle text, mana cost, type, legality, pricing, and image. Handles double-faced and split cards.
- **`scryfall_search`** - Search using [Scryfall syntax](https://scryfall.com/docs/syntax) (e.g. `c:ug type:creature cmc<=3`).
- **`scryfall_rulings`** - Official rulings for a card.

### Deck Tools
- **`import_deck`** - Import a deck from an Archidekt or Moxfield URL.
- **`analyze_deck`** - Import and analyze a deck: mana curve, color distribution, type breakdown, mana base evaluation, and EDHRec-based card suggestions.
- **`build_deck`** - Build a full 100-card Commander deck from a card name. Supports budget tiers and theme filtering. If the card isn't a commander, suggests commanders that use it.

### Pricing
- **`price_deck`** - Price a deck from Archidekt/Moxfield on both TCGPlayer and Card Kingdom. Compares total cost and card availability with a recommendation.

### Rules
- **`mtg_rules`** - Search the MTG Comprehensive Rules by keyword or rule number. Covers 3,100+ rules and 730+ glossary entries. Auto-updates when Wizards publishes new rules.

## Installation

Requires [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/GrooGruxKing27/mtg-commander-MCP.git
cd mtg-commander-MCP
uv sync
```

## Claude Desktop Setup

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mtg-commander": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/mtg-commander-MCP",
        "mtg-commander-mcp"
      ]
    }
  }
}
```

Restart Claude Desktop to pick up the new server.

## Claude Code Setup

Add to your Claude Code settings or `.claude/settings.json`:

```json
{
  "mcpServers": {
    "mtg-commander": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/mtg-commander-MCP",
        "mtg-commander-mcp"
      ]
    }
  }
}
```

## Testing

Run the MCP Inspector:

```bash
uv run mcp dev src/mtg_commander_mcp/server.py
```

## Budget Tiers (build_deck)

| Tier | Per-Card Cap |
|---|---|
| `budget` | < $2 |
| `modest` | < $10 |
| `no_limit` | No filter |

## Color Identity Filters (edhrec_search_commanders)

Mono: `white`, `blue`, `black`, `red`, `green`, `colorless`

Two-color: `azorius`, `dimir`, `rakdos`, `gruul`, `selesnya`, `orzhov`, `izzet`, `golgari`, `boros`, `simic`

Three-color: `esper`, `grixis`, `jund`, `naya`, `bant`, `abzan`, `jeskai`, `sultai`, `mardu`, `temur`

Five-color: `five-color`

## Dependencies

- `mcp[cli]` - Model Context Protocol SDK
- `httpx` - Async HTTP client
- `cloudscraper` - Cloudflare bypass for Moxfield
