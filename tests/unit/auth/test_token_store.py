"""Unit tests for specmcp.auth.token_store."""

from __future__ import annotations

import time

import pytest

from specmcp.auth.token_store import InMemoryTokenStore, OAuthTokens
from specmcp.config import SensitiveStr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tokens(
    access_token: str = "at-abc",
    refresh_token: str | None = "rt-xyz",
    expires_at: float | None = None,
    scope: str = "read write",
) -> OAuthTokens:
    return OAuthTokens(
        access_token=SensitiveStr(access_token),
        refresh_token=SensitiveStr(refresh_token) if refresh_token else None,
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# OAuthTokens dataclass
# ---------------------------------------------------------------------------


def test_oauth_tokens_sensitive_str_redacted():
    """access_token and refresh_token must be redacted in str() / repr()."""
    tokens = _make_tokens(access_token="secret-at", refresh_token="secret-rt")
    assert "secret-at" not in str(tokens.access_token)
    assert "secret-at" not in repr(tokens.access_token)
    assert "secret-rt" not in str(tokens.refresh_token)  # type: ignore[arg-type]


def test_oauth_tokens_reveal_returns_actual_value():
    """SensitiveStr.reveal() returns the real token value."""
    tokens = _make_tokens(access_token="real-access", refresh_token="real-refresh")
    assert tokens.access_token.reveal() == "real-access"
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.reveal() == "real-refresh"


def test_oauth_tokens_no_refresh_token():
    """refresh_token may be None."""
    tokens = _make_tokens(refresh_token=None)
    assert tokens.refresh_token is None


def test_oauth_tokens_is_expired_valid():
    """is_expired() returns False when token has more than buffer_seconds remaining."""
    tokens = _make_tokens(expires_at=time.time() + 3600)
    assert not tokens.is_expired(buffer_seconds=60.0)


def test_oauth_tokens_is_expired_within_buffer():
    """is_expired() returns True when token expires within the buffer window."""
    tokens = _make_tokens(expires_at=time.time() + 30)  # expires in 30s
    assert tokens.is_expired(buffer_seconds=60.0)  # buffer is 60s


def test_oauth_tokens_is_expired_unknown_expiry():
    """is_expired() returns False when expires_at == 0.0 (unknown expiry)."""
    tokens = _make_tokens(expires_at=0.0)
    assert not tokens.is_expired()


def test_oauth_tokens_from_token_response_full():
    """from_token_response() builds OAuthTokens from a full token response dict."""
    data = {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "expires_in": 3600,
        "scope": "read",
        "token_type": "Bearer",
    }
    tokens = OAuthTokens.from_token_response(data)
    assert tokens.access_token.reveal() == "new-at"
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.reveal() == "new-rt"
    assert tokens.scope == "read"
    assert tokens.token_type == "Bearer"
    assert tokens.expires_at > time.time()


def test_oauth_tokens_from_token_response_uses_new_refresh_token():
    """from_token_response() uses the NEW refresh_token from the response, not the old one."""
    old_rt = SensitiveStr("old-refresh")
    data = {"access_token": "new-at", "refresh_token": "brand-new-rt", "expires_in": 600}
    tokens = OAuthTokens.from_token_response(data, existing_refresh_token=old_rt)
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.reveal() == "brand-new-rt"


def test_oauth_tokens_from_token_response_falls_back_to_existing_rt():
    """from_token_response() falls back to existing_refresh_token if server omits rt."""
    old_rt = SensitiveStr("keep-this-rt")
    data = {"access_token": "new-at", "expires_in": 600}  # no refresh_token in response
    tokens = OAuthTokens.from_token_response(data, existing_refresh_token=old_rt)
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.reveal() == "keep-this-rt"


def test_oauth_tokens_from_token_response_no_expires_in():
    """from_token_response() sets expires_at=0.0 when expires_in is absent."""
    data = {"access_token": "at"}
    tokens = OAuthTokens.from_token_response(data)
    assert tokens.expires_at == 0.0


# ---------------------------------------------------------------------------
# InMemoryTokenStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_get_returns_none_before_save():
    """get() returns None for an unknown session."""
    store = InMemoryTokenStore()
    result = await store.get("unknown-session")
    assert result is None


@pytest.mark.asyncio
async def test_in_memory_save_then_get_round_trips():
    """save() then get() returns the same OAuthTokens."""
    store = InMemoryTokenStore()
    tokens = _make_tokens(access_token="roundtrip-token")
    await store.save("session-1", tokens)
    retrieved = await store.get("session-1")
    assert retrieved is tokens


@pytest.mark.asyncio
async def test_in_memory_save_overwrites_existing():
    """Calling save() twice replaces the previous entry."""
    store = InMemoryTokenStore()
    t1 = _make_tokens(access_token="first")
    t2 = _make_tokens(access_token="second")
    await store.save("session-1", t1)
    await store.save("session-1", t2)
    result = await store.get("session-1")
    assert result is t2


@pytest.mark.asyncio
async def test_in_memory_delete_removes_entry():
    """delete() removes a session's tokens; subsequent get() returns None."""
    store = InMemoryTokenStore()
    await store.save("session-1", _make_tokens())
    await store.delete("session-1")
    assert await store.get("session-1") is None


@pytest.mark.asyncio
async def test_in_memory_delete_noop_for_missing_session():
    """delete() on an unknown session ID is a no-op (does not raise)."""
    store = InMemoryTokenStore()
    await store.delete("nonexistent-session")  # should not raise


@pytest.mark.asyncio
async def test_in_memory_all_sessions_empty():
    """all_sessions() returns empty list when store is empty."""
    store = InMemoryTokenStore()
    assert await store.all_sessions() == []


@pytest.mark.asyncio
async def test_in_memory_all_sessions_lists_all():
    """all_sessions() returns all saved session IDs."""
    store = InMemoryTokenStore()
    await store.save("s1", _make_tokens())
    await store.save("s2", _make_tokens())
    await store.save("s3", _make_tokens())
    sessions = await store.all_sessions()
    assert set(sessions) == {"s1", "s2", "s3"}


@pytest.mark.asyncio
async def test_in_memory_all_sessions_after_delete():
    """all_sessions() does not include deleted sessions."""
    store = InMemoryTokenStore()
    await store.save("s1", _make_tokens())
    await store.save("s2", _make_tokens())
    await store.delete("s1")
    sessions = await store.all_sessions()
    assert sessions == ["s2"]
