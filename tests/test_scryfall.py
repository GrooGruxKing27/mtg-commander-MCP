"""Tests for the ScryfallClient hang/wedge fixes (v0.6.1).

All HTTP traffic is routed through httpx.MockTransport — no network. The
transport is installed by replacing ``_get_client`` on the instance with a
version that preserves the real lazy-build/reset semantics, so the
self-healing behavior (``_reset_client`` discarding the client, the next
call rebuilding it) is exercised for real.
"""

import asyncio
import json

import httpx
import pytest

from mtg_commander_mcp.clients.scryfall import ScryfallClient, ScryfallError


def install_transport(sc: ScryfallClient, handler) -> None:
    def _get_client() -> httpx.AsyncClient:
        if sc._client is None:
            sc._client = httpx.AsyncClient(
                base_url=sc.BASE_URL,
                transport=httpx.MockTransport(handler),
                timeout=1.0,
            )
        return sc._client

    sc._get_client = _get_client
    sc.REQUEST_DELAY = 0  # no throttle sleeps in tests


def search_payload(*names: str) -> dict:
    return {"data": [{"name": n, "prices": {}} for n in names]}


class RecordingSleep:
    """Replaces asyncio.sleep so retry back-off is observable and instant."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay, result=None):
        self.delays.append(delay)
        return result


@pytest.fixture
def fake_sleep(monkeypatch):
    rec = RecordingSleep()
    monkeypatch.setattr(asyncio, "sleep", rec)
    return rec


# ---------------------------------------------------------------------------
# Baseline behavior through the new request path
# ---------------------------------------------------------------------------


async def test_search_returns_cards_and_respects_limit():
    sc = ScryfallClient()
    install_transport(
        sc, lambda req: httpx.Response(200, json=search_payload("A", "B", "C"))
    )
    cards = await sc.search("t:instant", limit=2)
    assert [c["name"] for c in cards] == ["A", "B"]


async def test_fetch_caches_by_path_and_params():
    sc = ScryfallClient()
    calls = []
    install_transport(
        sc,
        lambda req: (calls.append(req.url.path), httpx.Response(200, json={"ok": 1}))[1],
    )
    assert await sc._fetch("/cards/named", params={"fuzzy": "Sol Ring"}) == {"ok": 1}
    assert await sc._fetch("/cards/named", params={"fuzzy": "Sol Ring"}) == {"ok": 1}
    assert len(calls) == 1  # second hit served from cache


async def test_404_surfaces_scryfall_details():
    sc = ScryfallClient()
    install_transport(
        sc, lambda req: httpx.Response(404, json={"details": "No card found"})
    )
    with pytest.raises(ScryfallError, match="No card found"):
        await sc.search("xyzzy")


# ---------------------------------------------------------------------------
# 429 retry loop: bounded waits, bounded attempts
# ---------------------------------------------------------------------------


async def test_429_retries_then_succeeds(fake_sleep):
    sc = ScryfallClient()
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=search_payload("Beast Within"))

    install_transport(sc, handler)
    cards = await sc.search("q")
    assert cards[0]["name"] == "Beast Within"
    assert len(attempts) == 3
    # default back-off: 1.0 + attempt * 2
    assert fake_sleep.delays == [1.0, 3.0]


async def test_retry_after_header_is_capped(fake_sleep):
    sc = ScryfallClient()
    seen = []

    def handler(req):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, json=search_payload("X"))

    install_transport(sc, handler)
    await sc.search("q")
    # A one-hour Retry-After must not sleep the tool call for an hour.
    assert fake_sleep.delays == [sc.MAX_RETRY_WAIT]


async def test_429_exhaustion_is_an_explicit_error(fake_sleep):
    sc = ScryfallClient()
    attempts = []

    def handler(req):
        attempts.append(1)
        return httpx.Response(429)

    install_transport(sc, handler)
    with pytest.raises(ScryfallError, match="rate limit exceeded"):
        await sc.search("q")
    assert len(attempts) == sc.MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Self-healing: client reset on timeout / transport error / deadline / cancel
# ---------------------------------------------------------------------------


async def test_timeout_raises_and_resets_client():
    sc = ScryfallClient()

    def handler(req):
        raise httpx.ReadTimeout("slow")

    install_transport(sc, handler)
    with pytest.raises(ScryfallError, match="timed out"):
        await sc.search("q")
    assert sc._client is None


async def test_transport_error_raises_and_resets_client():
    sc = ScryfallClient()

    def handler(req):
        raise httpx.ConnectError("refused")

    install_transport(sc, handler)
    with pytest.raises(ScryfallError, match="connection error"):
        await sc.search("q")
    assert sc._client is None


async def test_deadline_fires_fast_and_resets_client():
    sc = ScryfallClient()
    sc.TOTAL_DEADLINE = 0.1

    async def handler(req):
        await asyncio.sleep(5)
        return httpx.Response(200, json=search_payload("never"))

    install_transport(sc, handler)
    start = asyncio.get_running_loop().time()
    with pytest.raises(ScryfallError, match="did not answer within"):
        await sc.search("q")
    assert asyncio.get_running_loop().time() - start < 1.0
    assert sc._client is None


async def test_cancellation_mid_request_resets_client():
    """The wedge trigger: MCP host cancels a slow call mid-flight."""
    sc = ScryfallClient()

    async def handler(req):
        await asyncio.sleep(5)
        return httpx.Response(200, json=search_payload("never"))

    install_transport(sc, handler)
    task = asyncio.create_task(sc.search("q"))
    await asyncio.sleep(0.05)  # let the request get in-flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sc._client is None


async def test_client_recovers_after_reset():
    """After a reset, the next call builds a fresh client and succeeds."""
    sc = ScryfallClient()
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("first call dies")
        return httpx.Response(200, json=search_payload("Sol Ring"))

    install_transport(sc, handler)
    with pytest.raises(ScryfallError):
        await sc.search("a")
    assert sc._client is None

    cards = await sc.search("b")
    assert cards[0]["name"] == "Sol Ring"
    assert sc._client is not None  # rebuilt lazily


# ---------------------------------------------------------------------------
# /cards/collection now goes through the hardened request path
# ---------------------------------------------------------------------------


def collection_handler(fail_first_with_429: bool = False):
    """Echo back a card for every identifier except 'Notarealcard'."""
    state = {"posts": 0}

    def handler(req):
        assert req.method == "POST"
        assert req.url.path == "/cards/collection"
        state["posts"] += 1
        if fail_first_with_429 and state["posts"] == 1:
            return httpx.Response(429)
        identifiers = json.loads(req.content)["identifiers"]
        data = [
            {"name": i["name"], "prices": {}}
            for i in identifiers
            if i["name"] != "Notarealcard"
        ]
        nf = [i for i in identifiers if i["name"] == "Notarealcard"]
        return httpx.Response(200, json={"data": data, "not_found": nf})

    handler.state = state
    return handler


async def test_get_collection_maps_found_and_not_found():
    sc = ScryfallClient()
    install_transport(sc, collection_handler())
    found, not_found = await sc.get_collection(["Sol Ring", "Notarealcard"])
    assert sorted(found) == ["Sol Ring"]
    assert not_found == ["Notarealcard"]


async def test_get_collection_batches_every_75_names():
    sc = ScryfallClient()
    handler = collection_handler()
    install_transport(sc, handler)
    names = [f"Card {i}" for i in range(80)]
    found, not_found = await sc.get_collection(names)
    assert handler.state["posts"] == 2
    assert len(found) == 80 and not_found == []


async def test_get_collection_retries_429(fake_sleep):
    sc = ScryfallClient()
    handler = collection_handler(fail_first_with_429=True)
    install_transport(sc, handler)
    found, _ = await sc.get_collection(["Sol Ring"])
    assert sorted(found) == ["Sol Ring"]
    assert handler.state["posts"] == 2  # 429 then success — previously fatal


@pytest.mark.asyncio
async def test_get_collection_time_budget_defers_and_resumes():
    """A zero budget defers uncached batches; a repeat call resumes from the
    per-batch cache instead of re-requesting what was already resolved."""
    sc = ScryfallClient()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        idents = json.loads(request.content)["identifiers"]
        calls.append(len(idents))
        return httpx.Response(
            200, json={"data": [{"name": i["name"]} for i in idents], "not_found": []}
        )

    install_transport(sc, handler)
    names = [f"Card {i:03d}" for i in range(160)]  # 3 batches of 75/75/10

    deferred: list[str] = []
    found, _ = await sc.get_collection(names, time_budget=-1, deferred=deferred)
    assert found == {} and len(deferred) == 160 and calls == []

    deferred = []
    found, _ = await sc.get_collection(names, time_budget=100, deferred=deferred)
    assert len(found) == 160 and deferred == [] and calls == [75, 75, 10]
