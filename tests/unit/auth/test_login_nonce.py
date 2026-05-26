"""Unit tests for specmcp.auth.login_nonce.LoginNonceStore."""

from __future__ import annotations

import asyncio

import pytest

from specmcp.auth.login_nonce import LoginNonceStore


@pytest.mark.asyncio
async def test_issue_returns_url_safe_string():
    """issue() returns a non-empty URL-safe string (no +, /, =)."""
    store = LoginNonceStore()
    nonce = await store.issue("session-1")
    assert isinstance(nonce, str)
    assert len(nonce) >= 43  # 32 random bytes → 43-char base64url
    # URL-safe base64: only alphanumeric, hyphen, underscore
    assert all(c.isalnum() or c in "-_" for c in nonce)


@pytest.mark.asyncio
async def test_issue_returns_unique_nonces():
    """Two calls to issue() must return different nonces."""
    store = LoginNonceStore()
    n1 = await store.issue("s1")
    n2 = await store.issue("s2")
    assert n1 != n2


@pytest.mark.asyncio
async def test_consume_valid_nonce_returns_session_id():
    """consume() returns the session_id that was mapped to the nonce."""
    store = LoginNonceStore()
    nonce = await store.issue("session-abc")
    result = await store.consume(nonce)
    assert result == "session-abc"


@pytest.mark.asyncio
async def test_consume_removes_nonce_single_use():
    """After consume(), the same nonce returns None (single-use)."""
    store = LoginNonceStore()
    nonce = await store.issue("session-xyz")
    first = await store.consume(nonce)
    second = await store.consume(nonce)
    assert first == "session-xyz"
    assert second is None


@pytest.mark.asyncio
async def test_consume_unknown_nonce_returns_none():
    """consume() on an unknown nonce returns None (not a KeyError)."""
    store = LoginNonceStore()
    result = await store.consume("completely-unknown-nonce")
    assert result is None


@pytest.mark.asyncio
async def test_pending_count_tracks_outstanding_nonces():
    """pending_count() reflects the number of active (unconsumed) nonces."""
    store = LoginNonceStore()
    assert await store.pending_count() == 0

    await store.issue("s1")
    await store.issue("s2")
    assert await store.pending_count() == 2

    n3 = await store.issue("s3")
    await store.consume(n3)
    assert await store.pending_count() == 2


@pytest.mark.asyncio
async def test_maxsize_evicts_oldest_entries():
    """When maxsize is exceeded, the oldest nonce is evicted (cachetools behaviour).

    This is a safety property: memory is bounded even under a flood of requests.
    The evicted nonce's login flow will simply fail at callback validation.
    """
    store = LoginNonceStore(maxsize=3, ttl=300)

    n1 = await store.issue("s1")
    n2 = await store.issue("s2")
    n3 = await store.issue("s3")
    # Adding a 4th evicts the oldest (n1)
    await store.issue("s4")

    # n1 should be gone
    assert await store.consume(n1) is None
    # n2, n3 may or may not be evicted depending on LRU order, but n4 must exist
    assert await store.pending_count() <= 3


@pytest.mark.asyncio
async def test_concurrent_issue_and_consume_are_safe():
    """Concurrent issue/consume calls do not corrupt state (lock protection)."""
    store = LoginNonceStore()

    # Issue 10 nonces concurrently
    session_ids = [f"session-{i}" for i in range(10)]
    nonces = await asyncio.gather(*[store.issue(sid) for sid in session_ids])

    # Consume all concurrently
    results = await asyncio.gather(*[store.consume(n) for n in nonces])

    # Each nonce maps to exactly one session
    assert sorted(results) == sorted(session_ids)

    # All consumed — store should be empty
    assert await store.pending_count() == 0


@pytest.mark.asyncio
async def test_expired_nonce_returns_none():
    """A nonce created with ttl=0.05 expires and consume() returns None."""
    store = LoginNonceStore(ttl=0.05)  # 50 ms TTL
    nonce = await store.issue("session-x")

    # Wait for the TTL to expire
    await asyncio.sleep(0.15)

    result = await store.consume(nonce)
    assert result is None
