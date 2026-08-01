"""Tests for the ``bounded`` tool-level backstop timeout (v0.6.2).

Added after a report that scryfall_search still hung ~4 minutes on a
specific query shape even with the client's own 45s internal deadline
(v0.6.1) in place. Direct testing against Scryfall and against the live
tool couldn't reproduce a query-specific hang, so this adds a second,
independent timeout layer at the tool boundary: it guarantees no
single-purpose lookup tool can silently run past TOOL_DEADLINE, even if a
future bug (in our code or a dependency) bypasses a client's own deadline.
"""

import asyncio

import pytest

from mtg_commander_mcp.server import bounded


async def test_fast_call_passes_through_unchanged():
    @bounded(seconds=1.0)
    async def quick(x):
        return {"cards": [x]}

    assert await quick("Sol Ring") == {"cards": ["Sol Ring"]}


async def test_slow_call_times_out_as_explicit_error_dict():
    @bounded(seconds=0.05)
    async def slow():
        await asyncio.sleep(5)
        return {"cards": ["never"]}

    result = await slow()
    assert result == {
        "error": "slow timed out after 0s — retry the call"
    }


async def test_timeout_cancels_the_underlying_call():
    """The wrapped coroutine must actually stop running, not leak in the
    background — otherwise a wedged call still ties up client state."""
    cancelled = False

    @bounded(seconds=0.05)
    async def slow():
        nonlocal cancelled
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise

    await slow()
    assert cancelled is True


async def test_exception_from_wrapped_call_propagates():
    @bounded(seconds=1.0)
    async def boom():
        raise ValueError("bad query")

    with pytest.raises(ValueError, match="bad query"):
        await boom()


async def test_preserves_name_and_docstring_for_mcp_introspection():
    @bounded()
    async def scryfall_search(query: str, limit: int = 20) -> dict:
        """Search for cards."""
        return {"cards": []}

    assert scryfall_search.__name__ == "scryfall_search"
    assert scryfall_search.__doc__ == "Search for cards."
    # inspect.signature must resolve through the wrapper to the original
    # params — this is what lets @mcp.tool() build the right JSON schema.
    import inspect

    sig = inspect.signature(scryfall_search)
    assert list(sig.parameters) == ["query", "limit"]
