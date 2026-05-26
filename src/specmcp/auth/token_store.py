"""OAuth token storage for specmcp."""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from specmcp.config import SensitiveStr


@dataclass
class OAuthTokens:
    """Access and refresh tokens for a single session."""

    access_token: SensitiveStr
    refresh_token: SensitiveStr | None
    expires_at: float  # Unix timestamp; 0.0 = server omitted expires_in
    token_type: str = "Bearer"
    scope: str = ""

    def is_expired(self, buffer_seconds: float = 60.0) -> bool:
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


class TokenStore(ABC):
    async def open(self) -> None:
        """Initialise the store (e.g. open DB connection). No-op by default."""

    async def close(self) -> None:
        """Tear down the store (e.g. close DB connection). No-op by default."""

    @abstractmethod
    async def get(self, session_id: str) -> OAuthTokens | None: ...

    @abstractmethod
    async def save(self, session_id: str, tokens: OAuthTokens) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...

    @abstractmethod
    async def all_sessions(self) -> list[str]: ...


class InMemoryTokenStore(TokenStore):
    """Dict-backed token store. Tokens are lost on process restart."""

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
    session_id      TEXT PRIMARY KEY,
    encrypted_blob  BLOB NOT NULL,
    updated_at      REAL NOT NULL
);
"""

_DERIVE_CONTEXT = "token_store_v1"


def _tokens_to_json(tokens: OAuthTokens) -> str:
    return json.dumps({
        "access_token": tokens.access_token.reveal(),
        "refresh_token": tokens.refresh_token.reveal() if tokens.refresh_token else None,
        "expires_at": tokens.expires_at,
        "token_type": tokens.token_type,
        "scope": tokens.scope,
    })


def _tokens_from_json(raw: str) -> OAuthTokens:
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

    Usage::

        store = SqliteTokenStore(Path("tokens.db"), encryption_key=key_bytes)
        async with store:
            await store.save("session-id", tokens)
    """

    def __init__(self, db_path: Path, encryption_key: bytes) -> None:
        from specmcp.auth.encryption import derive_key
        self._db_path = db_path
        self._derived_key = derive_key(encryption_key, _DERIVE_CONTEXT)
        self._db: object = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SqliteTokenStore":
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        import aiosqlite
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(_SCHEMA)  # type: ignore[union-attr]
        await self._db.commit()  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()  # type: ignore[union-attr]
            self._db = None

    async def get(self, session_id: str) -> OAuthTokens | None:
        from specmcp.auth.encryption import decrypt
        if self._db is None:
            raise RuntimeError("SqliteTokenStore not open")
        async with self._lock:
            async with self._db.execute(  # type: ignore[union-attr]
                "SELECT encrypted_blob FROM oauth_tokens WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return _tokens_from_json(decrypt(bytes(row[0]), self._derived_key))
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    async def save(self, session_id: str, tokens: OAuthTokens) -> None:
        from specmcp.auth.encryption import encrypt
        if self._db is None:
            raise RuntimeError("SqliteTokenStore not open")
        blob = encrypt(_tokens_to_json(tokens), self._derived_key)
        async with self._lock:
            await self._db.execute(  # type: ignore[union-attr]
                """
                INSERT INTO oauth_tokens (session_id, encrypted_blob, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    encrypted_blob = excluded.encrypted_blob,
                    updated_at = excluded.updated_at
                """,
                (session_id, blob, time.time()),
            )
            await self._db.commit()  # type: ignore[union-attr]

    async def delete(self, session_id: str) -> None:
        if self._db is None:
            raise RuntimeError("SqliteTokenStore not open")
        async with self._lock:
            await self._db.execute(  # type: ignore[union-attr]
                "DELETE FROM oauth_tokens WHERE session_id = ?", (session_id,)
            )
            await self._db.commit()  # type: ignore[union-attr]

    async def all_sessions(self) -> list[str]:
        if self._db is None:
            raise RuntimeError("SqliteTokenStore not open")
        async with self._lock:
            async with self._db.execute(  # type: ignore[union-attr]
                "SELECT session_id FROM oauth_tokens"
            ) as cursor:
                rows = await cursor.fetchall()
        return [row[0] for row in rows]
