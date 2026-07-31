"""Tests for the EDHRecClient hardening (v0.6.1): bounded redirects,
redirect caching, and self-healing client resets."""

import asyncio

import httpx
import pytest

from mtg_commander_mcp.clients.edhrec import EDHRecClient, EDHRecError


def install_transport(ec: EDHRecClient, handler) -> None:
    def _get_client() -> httpx.AsyncClient:
        if ec._client is None:
            ec._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=1.0
            )
        return ec._client

    ec._get_client = _get_client


async def test_redirect_is_followed_and_cached_under_original_path():
    ec = EDHRecClient()
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/pages/commanders/old-name.json":
            return httpx.Response(200, json={"redirect": "commanders/new-name"})
        return httpx.Response(200, json={"header": "New Name"})

    install_transport(ec, handler)
    data = await ec._fetch("commanders/old-name")
    assert data == {"header": "New Name"}
    assert len(calls) == 2

    # Second fetch of the *original* path must not re-walk the redirect.
    assert await ec._fetch("commanders/old-name") == {"header": "New Name"}
    assert len(calls) == 2


async def test_redirect_loop_is_bounded():
    ec = EDHRecClient()
    calls = []

    def handler(req):
        calls.append(req.url.path)
        target = "commanders/b" if req.url.path.endswith("/a.json") else "commanders/a"
        return httpx.Response(200, json={"redirect": target})

    install_transport(ec, handler)
    with pytest.raises(EDHRecError, match="redirect loop"):
        await ec._fetch("commanders/a")
    # Previously this recursed without bound.
    assert len(calls) <= ec.MAX_REDIRECTS + 1


async def test_timeout_resets_client():
    ec = EDHRecClient()

    def handler(req):
        raise httpx.ReadTimeout("slow")

    install_transport(ec, handler)
    with pytest.raises(EDHRecError, match="timed out"):
        await ec._fetch("commanders/foo")
    assert ec._client is None


async def test_transport_error_resets_client():
    ec = EDHRecClient()

    def handler(req):
        raise httpx.ConnectError("refused")

    install_transport(ec, handler)
    with pytest.raises(EDHRecError, match="connection error"):
        await ec._fetch("commanders/foo")
    assert ec._client is None


async def test_cancellation_mid_request_resets_client():
    ec = EDHRecClient()

    async def handler(req):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    install_transport(ec, handler)
    task = asyncio.create_task(ec._fetch("commanders/foo"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ec._client is None


async def test_recovers_after_reset():
    ec = EDHRecClient()
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("first call dies")
        return httpx.Response(200, json={"header": "Atraxa"})

    install_transport(ec, handler)
    with pytest.raises(EDHRecError):
        await ec._fetch("commanders/atraxa")
    assert await ec._fetch("commanders/atraxa") == {"header": "Atraxa"}
