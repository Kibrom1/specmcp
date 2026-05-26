"""Unit tests for specmcp.auth.token_store."""

from __future__ import annotations

import os
import time

import pytest

from specmcp.auth.token_store import InMemoryTokenStore, OAuthTokens, SqliteTokenStore
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
async def test_inmemory_get_returns_none_for_unknown():
    store = InMemoryTokenStore()
    assert await store.get("unknown") is None


@pytest.mark.asyncio
async def test_inmemory_save_and_get_round_trips():
    store = InMemoryTokenStore()
    tokens = _make_tokens("at-42")
    await store.save("sess-1", tokens)
    result = await store.get("sess-1")
    assert result is not None
    assert result.access_token.reveal() == "at-42"


@pytest.mark.asyncio
async def test_inmemory_save_overwrites():
    store = InMemoryTokenStore()
    await store.save("sess-1", _make_tokens("old-token"))
    await store.save("sess-1", _make_tokens("new-token"))
    result = await store.get("sess-1")
    assert result is not None
    assert result.access_token.reveal() == "new-token"


@pytest.mark.asyncio
async def test_inmemory_delete_removes_entry():
    store = InMemoryTokenStore()
    await store.save("sess-1", _make_tokens())
    await store.delete("sess-1")
    assert await store.get("sess-1") is None


@pytest.mark.asyncio
async def test_inmemory_delete_nonexistent_is_noop():
    store = InMemoryTokenStore()
    await store.delete("does-not-exist")  # must not raise


@pytest.mark.asyncio
async def test_inmemory_all_sessions():
    store = InMemoryTokenStore()
    await store.save("sess-a", _make_tokens())
    await store.save("sess-b", _make_tokens())
    sessions = await store.all_sessions()
    assert set(sessions) == {"sess-a", "sess-b"}


@pytest.mark.asyncio
async def test_inmemory_all_sessions_empty():
    store = InMemoryTokenStore()
    assert await store.all_sessions() == []


# ---------------------------------------------------------------------------
# SqliteTokenStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_get_returns_none_for_unknown(tmp_path):
    key = os.urandom(32)
    async with SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key) as store:
        assert await store.get("unknown") is None


@pytest.mark.asyncio
async def test_sqlite_save_and_get_round_trips(tmp_path):
    key = os.urandom(32)
    async with SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key) as store:
        tokens = _make_tokens("sqlite-token", refresh_token="rt-1")
        await store.save("sess-1", tokens)
        result = await store.get("sess-1")

    assert result is not None
    assert result.access_token.reveal() == "sqlite-token"
    assert result.refresh_token is not None
    assert result.refresh_token.reveal() == "rt-1"


@pytest.mark.asyncio
async def test_sqlite_persists_across_opens(tmp_path):
    """Tokens saved in one store session survive close/reopen."""
    db = tmp_path / "tokens.db"
    key = os.urandom(32)

    async with SqliteTokenStore(db, encryption_key=key) as store:
        await store.save("sess-x", _make_tokens("persisted-token"))

    async with SqliteTokenStore(db, encryption_key=key) as store:
        result = await store.get("sess-x")

    assert result is not None
    assert result.access_token.reveal() == "persisted-token"


@pytest.mark.asyncio
async def test_sqlite_overwrite(tmp_path):
    key = os.urandom(32)
    async with SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key) as store:
        await store.save("sess-1", _make_tokens("old"))
        await store.save("sess-1", _make_tokens("new"))
        result = await store.get("sess-1")

    assert result is not None
    assert result.access_token.reveal() == "new"


@pytest.mark.asyncio
async def test_sqlite_delete(tmp_path):
    key = os.urandom(32)
    async with SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key) as store:
        await store.save("sess-1", _make_tokens())
        await store.delete("sess-1")
        assert await store.get("sess-1") is None


@pytest.mark.asyncio
async def test_sqlite_all_sessions(tmp_path):
    key = os.urandom(32)
    async with SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key) as store:
        await store.save("sess-a", _make_tokens())
        await store.save("sess-b", _make_tokens())
        sessions = await store.all_sessions()

    assert set(sessions) == {"sess-a", "sess-b"}


@pytest.mark.asyncio
async def test_sqlite_wrong_key_returns_none(tmp_path):
    """Fetching with the wrong key returns None (bad ciphertext → silently dropped)."""
    db = tmp_path / "tokens.db"
    key1 = os.urandom(32)
    key2 = os.urandom(32)

    async with SqliteTokenStore(db, encryption_key=key1) as store:
        await store.save("sess-1", _make_tokens("secret"))

    async with SqliteTokenStore(db, encryption_key=key2) as store:
        result = await store.get("sess-1")

    assert result is None  # decryption failure → returns None, not raises


@pytest.mark.asyncio
async def test_sqlite_raises_if_not_open(tmp_path):
    key = os.urandom(32)
    store = SqliteTokenStore(tmp_path / "tokens.db", encryption_key=key)

    with pytest.raises(RuntimeError, match="not open"):
        await store.get("any")


@pytest.mark.asyncio
async def test_sqlite_blob_is_opaque(tmp_path):
    """Raw bytes in the SQLite file must not contain the plaintext token."""
    import sqlite3

    db = tmp_path / "tokens.db"
    key = os.urandom(32)

    async with SqliteTokenStore(db, encryption_key=key) as store:
        await store.save("sess-1", _make_tokens("super-secret-access-token"))

    # Read raw blob from SQLite directly (bypassing our store)
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT encrypted_blob FROM oauth_tokens WHERE session_id = 'sess-1'").fetchone()
    conn.close()

    assert row is not None
    raw_bytes = bytes(row[0])
    assert b"super-secret-access-token" not in raw_bytes
