"""
OAuth token storage for specmcp.

TokenStore is an abstract persistent store for per-session OAuth tokens.
Two implementations are provided:

  InMemoryTokenStore  — dict-backed, no I/O, cleared on restart.
                        Use for dev, single-session, and testing.

  SqliteTokenStore    — aiosqlite-backed, AES-256-GCM encrypted blobs.
                        Use for production multi-user deployments.

The OAuthTokens dataclass carries the access and refresh tokens as SensitiveStr
so they are redacted in logs and reprs. Unlike CachedToken (which is memory-only
and uses plain str), OAuthTokens may be persisted to disk in Phase 3 — the
SensitiveStr + encryption-at-rest combination provides defence in depth.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from specmcp.config import SensitiveStr


# ---------------------------------------------------------------------------
# Token data model
# ---------------------------------------------------------------------------


@dataclass
class OAuthTokens:
    """Access and refresh tokens for a single session.

    Attributes:
        access_token: The bearer token injected into upstream API requests.
            Stored as SensitiveStr; revealed only at injection time.
        refresh_token: The refresh token for obtaining a new access token.
            None if the authorization server did not issue one.
            IMPORTANT: updated on every successful refresh (servers that rotate
            refresh tokens return a new value — storing the old one will cause
            invalid_grant on the next refresh).
        expires_at: Unix timestamp at which the access token expires.
            0.0 means the server did not provide expires_in — treat as unknown
            and fall back to the 401-triggered refresh path.
        token_type: Usually "Bearer". Stored for completeness.
        scope: Scope string as returned by the authorization server.
            May be narrower than the requested scope (see §9.3).
    """

    access_token: SensitiveStr
    refresh_token: SensitiveStr | None
    expires_at: float  # 0.0 means unknown
    token_type: str = "Bearer"
    scope: str = ""

    def is_expired(self, buffer_seconds: float = 60.0) -> bool:
        """Return True if the token expires within *buffer_seconds*.

        Returns False when expires_at == 0.0 (expiry unknown — server omitted
        expires_in). In that case the caller should rely on the 401 fallback path.
        """
        if self.expires_at == 0.0:
            return False
        return time.time() >= self.expires_at - buffer_seconds

    @classmethod
    def from_token_response(
        cls,
        data: dict,
        *,
        existing_refresh_token: SensitiveStr | None = None,
    ) -> "OAuthTokens":
        """Build OAuthTokens from a token endpoint JSON response dict.

        Uses the new refresh_token from the response if present; falls back to
        *existing_refresh_token* only for servers that don't rotate (non-standard).
        """
        return cls(
            access_token=SensitiveStr(data["access_token"]),
            refresh_token=(
                SensitiveStr(data["refresh_token"])
                if "refresh_token" in data
                else existing_refresh_token
            ),
            expires_at=(
                time.time() + float(data["expires_in"])
                if "expires_in" in data
                else 0.0
            ),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
        )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class TokenStore(ABC):
    """Abstract store for per-session OAuth tokens.

    All methods are async to support both the in-memory and SQLite backends
    without changing the calling code.
    """

    @abstractmethod
    async def get(self, session_id: str) -> OAuthTokens | None:
        """Return the tokens for *session_id*, or None if not found."""

    @abstractmethod
    async def save(self, session_id: str, tokens: OAuthTokens) -> None:
        """Persist *tokens* for *session_id*, replacing any existing entry."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove the token entry for *session_id* (no-op if not found)."""

    @abstractmethod
    async def all_sessions(self) -> list[str]:
        """Return all session IDs that have stored tokens."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTokenStore(TokenStore):
    """Dict-backed token store. Tokens are lost on process restart.

    Thread-safety: protected by a single asyncio.Lock so concurrent refreshes
    within the same event loop do not corrupt state. Per-session locking is the
    caller's responsibility (see _refresh_locks in oauth2_authcode.py).
    """

    def __init__(self) -> None:
        self._store: dict[str, OAuthTokens] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> OAuthTokens | None:
        async with self._lock:
            return self._store.get(session_id)

    async def save(self, session_id: str, tokens: OAuthTokens) -> None:
        async with self._lock:
            self._store[session_id] = tokens

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._store.pop(session_id, None)

    async def all_sessions(self) -> list[str]:
        async with self._lock:
            return list(self._store.keys())


# ---------------------------------------------------------------------------
# SQLite + AES-256-GCM implementation
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    session_id   TEXT PRIMARY KEY,
    encrypted_blob  BLOB NOT NULL,
    updated_at   REAL NOT NULL
);
"""

_DERIVE_CONTEXT = "token_store_v1"


def _tokens_to_json(tokens: OAuthTokens) -> str:
    """Serialise OAuthTokens to a JSON string for encryption.

    SensitiveStr fields are stored as their real values inside the encrypted
    blob — the AES-256-GCM envelope provides confidentiality.
    """
    return json.dumps({
        "access_token": tokens.access_token.reveal(),
        "refresh_token": tokens.refresh_token.reveal() if tokens.refresh_token else None,
        "expires_at": tokens.expires_at,
        "token_type": tokens.token_type,
        "scope": tokens.scope,
    })


def _tokens_from_json(raw: str) -> OAuthTokens:
    """Deserialise OAuthTokens from the decrypted JSON string."""
    data = json.loads(raw)
    return OAuthTokens(
        access_token=SensitiveStr(data["access_token"]),
        refresh_token=SensitiveStr(data["refresh_token"]) if data.get("refresh_token") else None,
        expires_at=float(data["expires_at"]),
        token_type=data.get("token_type", "Bearer"),
        scope=data.get("scope", ""),
    )


class SqliteTokenStore(TokenStore):
    """SQLite-backed token store with AES-256-GCM encryption at rest.

    Each row stores a single session's tokens as an encrypted blob. The
    plaintext is never written to disk — only the AES-256-GCM ciphertext.

    Key derivation:
        The master key is passed as *encryption_key* bytes. A sub-key is
        derived via ``derive_key(encryption_key, "token_store_v1")`` so that
        the master key can be rotated without changing the derivation context.

    Usage::

        store = SqliteTokenStore(
            db_path=Path("~/.specmcp/tokens.db").expanduser(),
            encryption_key=bytes.fromhex(os.environ["TOKEN_STORE_KEY"]),
        )
        async with store:
            await store.save("session-id", tokens)
            tokens = await store.get("session-id")
    """

    def __init__(self, db_path: Path, encryption_key: bytes) -> None:
        from specmcp.auth.encryption import derive_key
        self._db_path = db_path
        self._derived_key = derive_key(encryption_key, _DERIVE_CONTEXT)
        self._db: "aiosqlite.Connection | None" = None  # type: ignore[name-defined]
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SqliteTokenStore":
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Open the database connection and create the schema if needed."""
        import aiosqlite
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _assert_open(self) -> "aiosqlite.Connection":  # type: ignore[name-defined]
        if self._db is None:
            raise RuntimeError(
                "SqliteTokenStore is not open. "
                "Use 'async with store:' or call 'await store.open()' first."
            )
        return self._db

    async def get(self, session_id: str) -> OAuthTokens | None:
        from specmcp.auth.encryption import decrypt
        db = self._assert_open()
        async with self._lock:
            async with db.execute(
                "SELECT encrypted_blob FROM oauth_tokens WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            plaintext = decrypt(bytes(row[0]), self._derived_key)
            return _tokens_from_json(plaintext)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            import structlog
            structlog.get_logger().error(
                "token_store_error",
                session_id=session_id,
                operation="get",
                error=type(exc).__name__,
            )
            return None

    async def save(self, session_id: str, tokens: OAuthTokens) -> None:
        from specmcp.auth.encryption import encrypt
        db = self._assert_open()
        plaintext = _tokens_to_json(tokens)
        blob = encrypt(plaintext, self._derived_key)
        async with self._lock:
            await db.execute(
                """
                INSERT INTO oauth_tokens (session_id, encrypted_blob, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    encrypted_blob = excluded.encrypted_blob,
                    updated_at = excluded.updated_at
                """,
                (session_id, blob, time.time()),
            )
            await db.commit()

    async def delete(self, session_id: str) -> None:
        db = self._assert_open()
        async with self._lock:
            await db.execute(
                "DELETE FROM oauth_tokens WHERE session_id = ?",
                (session_id,),
            )
            await db.commit()

    async def all_sessions(self) -> list[str]:
        db = self._assert_open()
        async with self._lock:
            async with db.execute("SELECT session_id FROM oauth_tokens") as cursor:
                rows = await cursor.fetchall()
        return [row[0] for row in rows]
