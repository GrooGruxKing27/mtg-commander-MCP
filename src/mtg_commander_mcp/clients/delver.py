"""Delver Lens CSV import.

Delver Lens (delverlab.com / mtg.delver.app) exports collections as CSV
with user-selected column presets — Delver-native, Moxfield, TCGPlayer,
Deckbox, MTGstand, etc. There is no public API; the web dashboard at
mtg.delver.dev/collection requires Firebase auth and is not a stable
integration target. CSV is the documented surface, so we parse that.

This module is a pure parser: it accepts raw CSV text, a local file
path, or an http(s):// URL, normalizes column variants via a
case-insensitive alias table, and returns a list of canonical row
dicts. Card-name resolution against Scryfall happens one layer up in
``collection.py``.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from typing import Iterable

import httpx

from mtg_commander_mcp import __version__

logger = logging.getLogger(__name__)


class DelverError(Exception):
    pass


# Case-insensitive header aliases. First match wins per column.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "card name", "card"),
    "set_code": ("set code", "edition", "set"),
    "collector_number": ("collector number", "card number", "number"),
    "quantity": ("quantity", "count", "qty"),
    "foil": ("foil", "printing", "finish"),
    "condition": ("condition", "cond"),
    "language": ("language", "lang"),
    "scryfall_id": ("scryfall id", "scryfall_id", "scryfallid"),
}

_FOIL_TRUE = {"foil", "true", "1", "yes", "y"}
_FOIL_FALSE = {"normal", "nonfoil", "non-foil", "false", "0", "no", "n", ""}


class DelverClient:
    """Pure CSV parser for Delver Lens (and compatible) exports.

    Stateful only insofar as it owns an httpx client for URL sources.
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": f"MTGCommanderMCP/{__version__}",
                    "Accept": "text/csv, text/plain, */*",
                },
                follow_redirects=True,
            )
        return self._client

    async def load(self, source: str) -> str:
        """Resolve `source` (URL / path / pasted text) to raw CSV text.

        Detection:
          1. starts with http(s):// → fetch
          2. is an existing file on disk → read
          3. contains a newline → treat as pasted CSV
          4. otherwise → error
        """
        if not source:
            raise DelverError("empty source")

        if source.startswith(("http://", "https://")):
            client = self._get_client()
            try:
                resp = await client.get(source)
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPStatusError as e:
                raise DelverError(
                    f"Failed to fetch CSV ({e.response.status_code}): {source}"
                ) from e
            except httpx.HTTPError as e:
                raise DelverError(f"Failed to fetch CSV: {e}") from e

        # Path detection — be conservative. We only treat it as a path if it
        # actually exists, so a one-line pasted CSV ("Sol Ring,1") that
        # happens to look filesystem-y doesn't get mistaken for a path.
        expanded = os.path.expanduser(source)
        if os.path.isfile(expanded):
            try:
                with open(expanded, encoding="utf-8-sig") as f:
                    return f.read()
            except OSError as e:
                raise DelverError(f"Failed to read file {expanded}: {e}") from e

        if "\n" in source or "\r" in source:
            return source

        raise DelverError(
            f"source is not a URL, an existing file, or pasted CSV text: {source!r}"
        )

    def parse(self, csv_text: str) -> list[dict]:
        """Parse CSV text into a list of canonical row dicts.

        Canonical row keys: name, set_code, collector_number, quantity,
        foil, condition, language, scryfall_id. Missing/blank cells
        become None (or default for `quantity` / `foil`).
        """
        if not csv_text or not csv_text.strip():
            raise DelverError("empty CSV")

        # Strip a UTF-8 BOM if present (Delver mobile exports include one).
        if csv_text.startswith("﻿"):
            csv_text = csv_text.lstrip("﻿")

        reader = csv.reader(io.StringIO(csv_text))
        try:
            header = next(reader)
        except StopIteration:
            raise DelverError("CSV has no header row")

        column_map = self._map_columns(header)
        if "name" not in column_map and "scryfall_id" not in column_map:
            raise DelverError(
                "CSV must contain at least a Name or Scryfall ID column "
                f"(got headers: {header!r})"
            )

        rows: list[dict] = []
        for line_no, raw in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw):
                continue  # skip blank lines

            row = self._normalize_row(raw, column_map, line_no=line_no)
            if row is None:
                continue
            rows.append(row)
        return rows

    def _map_columns(self, header: list[str]) -> dict[str, int]:
        """Map canonical field name → header column index."""
        normalized = [h.strip().lower() for h in header]
        mapping: dict[str, int] = {}
        for canonical, aliases in _HEADER_ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    mapping[canonical] = normalized.index(alias)
                    break
        return mapping

    def _normalize_row(
        self, raw: list[str], col: dict[str, int], *, line_no: int
    ) -> dict | None:
        def _get(key: str) -> str | None:
            idx = col.get(key)
            if idx is None or idx >= len(raw):
                return None
            val = raw[idx].strip()
            return val if val else None

        name = _get("name")
        scryfall_id = _get("scryfall_id")
        if not name and not scryfall_id:
            logger.warning(
                "Skipping CSV row %d: no Name or Scryfall ID", line_no
            )
            return None

        try:
            quantity = int(_get("quantity") or "1")
        except ValueError:
            quantity = 1
        if quantity <= 0:
            return None

        foil_raw = (_get("foil") or "").lower()
        if foil_raw in _FOIL_TRUE:
            foil = True
        elif foil_raw in _FOIL_FALSE:
            foil = False
        else:
            # Unknown foil indicator — assume non-foil but log so unusual
            # exports (e.g. "Etched", "Gilded") aren't silently miscategorized.
            logger.warning(
                "Unrecognized foil value %r on line %d; treating as non-foil",
                foil_raw, line_no,
            )
            foil = False

        condition = _get("condition")
        if condition:
            condition = condition.upper()

        language = _get("language") or "en"

        return {
            "name": name,
            "set_code": (_get("set_code") or "").lower() or None,
            "collector_number": _get("collector_number"),
            "quantity": quantity,
            "foil": foil,
            "condition": condition,
            "language": language,
            "scryfall_id": scryfall_id,
        }

    async def parse_source(self, source: str) -> list[dict]:
        """Convenience: load + parse in one call."""
        return self.parse(await self.load(source))


def aggregate_by_name(rows: Iterable[dict]) -> dict[str, int]:
    """Sum quantities per Scryfall-canonical name.

    Used by the allocation math. Two `Sol Ring`s in different sets
    aggregate; printing-aware allocation is a future enhancement.
    """
    totals: dict[str, int] = {}
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        totals[name] = totals.get(name, 0) + int(r.get("quantity", 1))
    return totals
