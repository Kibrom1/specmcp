"""Login nonce store for OAuth Authorization Code flow.

A nonce is issued when an MCP session needs to authenticate. It is a
single-use, short-lived secret that maps a login URL's nonce parameter
back to the originating session_id. Once consumed the nonce is deleted.

Security properties:
  - Nonces are generated with secrets.token_urlsafe(32) (256-bit entropy).
  - Nonces expire after ``ttl`` seconds (default 300 = 5 minutes).
  - Consumption is atomic: concurrent calls will never both succeed.
  - The store is bounded (default maxsize=10_000) to prevent unbounded growth.
"""

from __future__ import annotations

import asyncio
import secrets

from cachetools import TTLCache


class LoginNonceStore:
    """TTL-bounded store mapping nonce -> session_id.

    Thread-safety: all public methods are async and protected by a single
    asyncio.Lock so they are safe under concurrent tool calls.
    """

    def __init__(self, *, maxsize: int = 10_000, ttl: float = 300.0) -> None:
        self._store: TTLCache[str, str] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = asyncio.Lock()

    async def issue(self, session_id: str) -> str:
        """Create a fresh nonce that maps to *session_id* and return it.

        The nonce is a 43-character URL-safe base64 string with 256-bit entropy.
        It expires after ``ttl`` seconds (default 5 minutes).
        """
        nonce = secrets.token_urlsafe(32)
        async with self._lock:
            self._store[nonce] = session_id
        return nonce

    async def consume(self, nonce: str) -> str | None:
        """Look up and delete *nonce*, returning the session_id or None.

        Returns None if the nonce was never issued, has already been used,
        or has expired. Each nonce can only be consumed once.
        """
        async with self._lock:
            return self._store.pop(nonce, None)

    async def pending_count(self) -> int:
        """Return the number of unexpired nonces currently in the store."""
        async with self._lock:
            return len(self._store)
