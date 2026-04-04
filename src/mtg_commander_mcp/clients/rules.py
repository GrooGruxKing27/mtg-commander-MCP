import os
import re
import json
from pathlib import Path
from functools import partial

import httpx

from mtg_commander_mcp.utils import Cache


class RulesError(Exception):
    pass


CACHE_DIR = Path.home() / ".cache" / "mtg-commander-mcp" / "rules"
RULES_PAGE_URL = "https://magic.wizards.com/en/rules"


class RulesClient:
    def __init__(self):
        self._rules: dict[str, str] | None = None  # rule_number -> text
        self._glossary: dict[str, str] | None = None  # term -> definition
        self._all_text: list[tuple[str, str]] | None = None  # (number, text) for search
        self._source_url: str | None = None
        self._cache = Cache(ttl=86400)  # 24h cache for rule lookups

    async def _ensure_loaded(self):
        """Load rules, downloading if needed."""
        if self._rules is not None:
            return

        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self):
        """Synchronous loading: check cache, download if needed."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        meta_path = CACHE_DIR / "meta.json"
        rules_path = CACHE_DIR / "rules.txt"

        # Check if we need to download
        need_download = True
        current_url = None

        if meta_path.exists() and rules_path.exists():
            meta = json.loads(meta_path.read_text())
            current_url = meta.get("source_url")
            # Try to find the latest URL
            try:
                latest_url = self._find_rules_url()
                need_download = latest_url != current_url
                if need_download:
                    current_url = latest_url
            except Exception:
                # Can't check for updates, use cached version
                need_download = False

        if need_download:
            if current_url is None:
                current_url = self._find_rules_url()
            self._download_rules(current_url, rules_path, meta_path)

        # Parse the rules file
        text = rules_path.read_text(encoding="utf-8", errors="replace")
        self._parse_rules(text)
        self._source_url = current_url

    def _find_rules_url(self) -> str:
        """Scrape the Wizards rules page to find the latest TXT download link."""
        resp = httpx.get(RULES_PAGE_URL, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()
        html = resp.text

        # Look for a .txt link to the comprehensive rules
        txt_match = re.search(
            r'href="(https?://media\.wizards\.com/[^"]*(?:MagicComp|CompRules)[^"]*\.txt)"',
            html, re.IGNORECASE
        )
        if txt_match:
            return txt_match.group(1)

        # Fallback: look for any rules download link
        any_match = re.search(
            r'href="(https?://media\.wizards\.com/[^"]*(?:MagicComp|CompRules)[^"]*)"',
            html, re.IGNORECASE
        )
        if any_match:
            return any_match.group(1)

        raise RulesError(
            "Could not find rules download link on the Wizards rules page. "
            "The page format may have changed."
        )

    def _download_rules(self, url: str, rules_path: Path, meta_path: Path):
        """Download the rules file and save metadata."""
        resp = httpx.get(url, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
        rules_path.write_bytes(resp.content)
        meta_path.write_text(json.dumps({"source_url": url}))

    def _parse_rules(self, text: str):
        """Parse the comprehensive rules into searchable structures."""
        self._rules = {}
        self._glossary = {}
        self._all_text = []

        lines = text.split("\n")
        current_rule = ""
        current_text = []
        in_glossary = False
        found_first_rule = False

        for line in lines:
            stripped = line.strip()

            # Detect glossary section — only after we've seen actual rules
            # (the table of contents also has "Glossary" early in the file)
            if stripped == "Glossary" and found_first_rule:
                in_glossary = True
                # Save any pending rule
                if current_rule and current_text:
                    full_text = " ".join(current_text)
                    self._rules[current_rule] = full_text
                    self._all_text.append((current_rule, full_text))
                current_rule = ""
                current_text = []
                continue

            if in_glossary:
                # Glossary format: Term on its own line, definition on
                # subsequent lines, entries separated by blank lines.
                if not stripped:
                    # Blank line = end of current glossary entry
                    if current_rule and current_text:
                        definition = " ".join(current_text)
                        self._glossary[current_rule] = definition
                        self._all_text.append((f"Glossary: {current_rule}", definition))
                        current_rule = ""
                        current_text = []
                elif not current_text and not current_rule:
                    # First non-blank line after a separator = new term
                    current_rule = stripped
                elif not current_text and current_rule:
                    # First line after term = start of definition
                    current_text.append(stripped)
                else:
                    # Continuation of definition
                    current_text.append(stripped)
                continue

            # Match rule numbers like "100.1", "702.5a", "100.1a"
            rule_match = re.match(r"^(\d{3}\.\d+[a-z]?)\b", stripped)
            if rule_match:
                found_first_rule = True
                # Save previous rule
                if current_rule and current_text:
                    full_text = " ".join(current_text)
                    self._rules[current_rule] = full_text
                    self._all_text.append((current_rule, full_text))

                current_rule = rule_match.group(1)
                rest = stripped[len(current_rule):].strip()
                current_text = [rest] if rest else []
            elif current_rule and stripped:
                current_text.append(stripped)

        # Save last rule
        if current_rule and current_text:
            full_text = " ".join(current_text)
            self._rules[current_rule] = full_text
            self._all_text.append((current_rule, full_text))

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search rules by number or keyword."""
        await self._ensure_loaded()

        # Check cache
        cache_key = f"search:{query}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        results = []

        # Exact rule number match
        if re.match(r"^\d{3}\.\d+[a-z]?$", query):
            text = self._rules.get(query)
            if text:
                results.append({"rule": query, "text": text})
            # Also find sub-rules
            prefix = query
            for rule_num, rule_text in (self._all_text or []):
                if rule_num.startswith(prefix) and rule_num != query:
                    results.append({"rule": rule_num, "text": rule_text})
                    if len(results) >= limit:
                        break
        else:
            # Keyword search
            query_lower = query.lower()
            for rule_num, rule_text in (self._all_text or []):
                if query_lower in rule_text.lower() or query_lower in rule_num.lower():
                    results.append({"rule": rule_num, "text": rule_text})
                    if len(results) >= limit:
                        break

        self._cache.set(cache_key, results)
        return results
