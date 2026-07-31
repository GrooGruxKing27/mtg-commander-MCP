"""Tests for the v0.6.1 hardening in the Archidekt and Moxfield clients,
and the shared background client-discard helper."""

import asyncio

import httpx
import pytest

from mtg_commander_mcp.clients import moxfield as moxfield_mod
from mtg_commander_mcp.clients.archidekt import ArchidektClient, ArchidektError
from mtg_commander_mcp.clients.moxfield import MoxfieldClient
from mtg_commander_mcp.utils import _pending_closes, discard_async_client


def install_transport(ac: ArchidektClient, handler) -> None:
    def _get_client() -> httpx.AsyncClient:
        if ac._client is None:
            ac._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=1.0
            )
        return ac._client

    ac._get_client = _get_client


async def test_archidekt_transport_error_resets_client():
    ac = ArchidektClient()

    def handler(req):
        raise httpx.ConnectError("refused")

    install_transport(ac, handler)
    with pytest.raises(ArchidektError, match="connection error"):
        await ac.get_deck("https://archidekt.com/decks/12345/some-deck")
    assert ac._client is None


async def test_archidekt_timeout_resets_client():
    ac = ArchidektClient()

    def handler(req):
        raise httpx.ReadTimeout("slow")

    install_transport(ac, handler)
    with pytest.raises(ArchidektError, match="timed out"):
        await ac.get_deck("https://archidekt.com/decks/12345/some-deck")
    assert ac._client is None


def test_moxfield_fetch_passes_explicit_timeout(monkeypatch):
    """A stalled Cloudflare handshake must not hang the tool forever."""
    captured = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"name": "deck"}

    class FakeScraper:
        def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResp()

    monkeypatch.setattr(
        moxfield_mod.cloudscraper, "create_scraper", lambda: FakeScraper()
    )
    data = MoxfieldClient()._fetch_sync("abc123")
    assert data == {"name": "deck"}
    assert captured["timeout"] == 30


async def test_discard_async_client_closes_in_background():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    )
    discard_async_client(client)
    for _ in range(100):
        if client.is_closed:
            break
        await asyncio.sleep(0.01)
    assert client.is_closed
    # The close task cleans itself out of the keep-alive set.
    await asyncio.sleep(0)
    assert not _pending_closes


def test_discard_async_client_without_loop_is_a_noop():
    # No running event loop (interpreter teardown path) — must not raise.
    discard_async_client(object())
