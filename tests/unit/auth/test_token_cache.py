"""
Unit tests for specmcp.auth.token_cache.TokenCache and CachedToken.

Tests:
  - First call fetches; subsequent calls hit cache
  - Expired token triggers refresh
  - Concurrent callers don't double-refresh (thundering-herd prevention)
  - invalidate() forces next call to refresh
  - CachedToken.is_expired() respects buffer
"""

from __future__ import annotations

import asyncio
import time

import anyio
import pytest

from specmcp.auth.token_cache import CachedToken, TokenCache
from specmcp.config import SensitiveStr


# ---------------------------------------------------------------------------
# CachedToken.is_expired()
# ---------------------------------------------------------------------------


def test_fresh_token_not_expired() -> None:
    token = CachedToken(
        access_token=SensitiveStr("tok"),
        expires_at=time.monotonic() + 3600,
    )
    assert not token.is_expired()


def test_expired_token() -> None:
    token = CachedToken(
        access_token=SensitiveStr("tok"),
        expires_at=time.monotonic() - 1,  # already past
    )
    assert token.is_expired()


def test_token_within_buffer_is_expired() -> None:
    # expires in 30 seconds, buffer is 60 — should be considered expired
    token = CachedToken(
        access_token=SensitiveStr("tok"),
        expires_at=time.monotonic() + 30,
    )
    assert token.is_expired(buffer_seconds=60.0)


def test_token_outside_buffer_not_expired() -> None:
    token = CachedToken(
        access_token=SensitiveStr("tok"),
        expires_at=time.monotonic() + 120,
    )
    assert not token.is_expired(buffer_seconds=60.0)


def test_cached_token_repr_hides_value() -> None:
    """SensitiveStr must not expose the raw token in repr/str of CachedToken."""
    token = CachedToken(
        access_token=SensitiveStr("super-secret-token"),
        expires_at=time.monotonic() + 3600,
    )
    assert "super-secret-token" not in repr(token)
    assert "super-secret-token" not in str(token)


# ---------------------------------------------------------------------------
# TokenCache.get_or_refresh()
# ---------------------------------------------------------------------------


def _make_token(value: str, ttl: float = 3600.0) -> CachedToken:
    return CachedToken(access_token=SensitiveStr(value), expires_at=time.monotonic() + ttl)


@pytest.mark.asyncio
async def test_first_call_fetches() -> None:
    cache = TokenCache()
    calls = 0

    async def refresh() -> CachedToken:
        nonlocal calls
        calls += 1
        return _make_token("token-v1")

    result = await cache.get_or_refresh(refresh)
    assert result == "token-v1"
    assert calls == 1


@pytest.mark.asyncio
async def test_second_call_hits_cache() -> None:
    cache = TokenCache()
    calls = 0

    async def refresh() -> CachedToken:
        nonlocal calls
        calls += 1
        return _make_token("token-v1")

    await cache.get_or_refresh(refresh)
    result = await cache.get_or_refresh(refresh)
    assert result == "token-v1"
    assert calls == 1  # refresh called only once


@pytest.mark.asyncio
async def test_expired_token_triggers_refresh() -> None:
    cache = TokenCache()
    cache._token = CachedToken(access_token=SensitiveStr("old"), expires_at=time.monotonic() - 1)
    calls = 0

    async def refresh() -> CachedToken:
        nonlocal calls
        calls += 1
        return _make_token("new-token")

    result = await cache.get_or_refresh(refresh)
    assert result == "new-token"
    assert calls == 1


@pytest.mark.asyncio
async def test_no_thundering_herd_on_concurrent_calls() -> None:
    """Multiple concurrent callers on an empty cache should trigger exactly
    one refresh, not N refreshes."""
    cache = TokenCache()
    calls = 0

    async def refresh() -> CachedToken:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # simulate network latency
        return _make_token("shared-token")

    async def caller() -> str:
        return await cache.get_or_refresh(refresh)

    results: list[str] = []

    async def collect(task: anyio.abc.TaskGroup) -> None:
        for _ in range(10):
            task.start_soon(lambda: cache.get_or_refresh(refresh))

    # Run 10 concurrent callers
    tokens: list[str] = []
    async with anyio.create_task_group() as tg:
        for _ in range(10):
            async def _call(cache: TokenCache = cache) -> None:
                t = await cache.get_or_refresh(refresh)
                tokens.append(t)
            tg.start_soon(_call)

    # All callers must get the same token value
    assert all(t == "shared-token" for t in tokens), tokens
    # Refresh must have been called exactly once
    assert calls == 1, f"Expected 1 refresh, got {calls}"


@pytest.mark.asyncio
async def test_invalidate_forces_refresh() -> None:
    cache = TokenCache()
    cache._token = _make_token("original")
    calls = 0

    async def refresh() -> CachedToken:
        nonlocal calls
        calls += 1
        return _make_token("refreshed")

    # First call hits cache
    result = await cache.get_or_refresh(refresh)
    assert result == "original"
    assert calls == 0

    # Invalidate
    cache.invalidate()

    # Next call must refresh
    result = await cache.get_or_refresh(refresh)
    assert result == "refreshed"
    assert calls == 1


@pytest.mark.asyncio
async def test_refresh_error_propagates() -> None:
    from specmcp.errors import TokenRefreshError

    cache = TokenCache()

    async def bad_refresh() -> CachedToken:
        raise TokenRefreshError("token endpoint down")

    with pytest.raises(TokenRefreshError, match="token endpoint down"):
        await cache.get_or_refresh(bad_refresh)

    # Cache should remain empty after a failed refresh
    assert cache._token is None
