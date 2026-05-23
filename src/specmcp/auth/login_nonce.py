"""
Single-use login nonces for the OAuth Authorization Code flow.

When a tool call requires auth but the session has no token, specmcp issues a
single-use nonce and returns a login URL to the LLM:

    "Visit https://<host>/auth/login?nonce=<token> to authenticate."

The LoginNonceStore maps nonce → session_id server-side. The session_id itself
is never included in the URL — this ensures the login URL is safe even if the
LLM provider logs conversation history. The nonce is consumed (deleted) on
first use and automatically expires after 5 minutes.

Security properties:
  - 256-bit entropy (secrets.token_urlsafe(32))
  - Single-use: consume() removes the nonce atomically
  - TTL: 300 seconds via cachetools.TTLCache
  - Bounded size: maxsize=10_000 prevents memory exhaustion under a flood of
    requests — excess entries cause the oldest nonces to be evicted (safe: the
    affected users simply need to request a new login link)
"""

from __future__ import annotations

import asyncio
import secrets

from cachetools import TTLCache


class LoginNonceStore:
    """Thread-safe, single-use, time-limited nonce store.

    Example usage::

        store = LoginNonceStore()
        nonce = await store.issue("session-uuid")
        # ... return nonce in login URL to LLM ...
        session_id = await store.consume(nonce)   # returns session_id and removes nonce
        assert await store.consume(nonce) is None  # single-use: second consume returns None
    """

    def __init__(
        self,
        *,
        maxsize: int = 10_000,
        ttl: int = 300,  # 5 minutes
    ) -> None:
        self._store: TTLCache[str, str] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = asyncio.Lock()

    async def issue(self, session_id: str) -> str:
        """Generate a new nonce and map it to *session_id*.

        Returns the nonce string (43+ chars, URL-safe, 256 bits of entropy).
        The nonce expires after the configured TTL and can only be consumed once.
        """
        nonce = secrets.token_urlsafe(32)
        async with self._lock:
            self._store[nonce] = session_id
        return nonce

    async def consume(self, nonce: str) -> str | None:
        """Return the session_id for *nonce* and delete it (single-use).

        Returns None if the nonce is unknown, already consumed, or expired.
        """
        async with self._lock:
            return self._store.pop(nonce, None)

    async def pending_count(self) -> int:
        """Return the number of currently active (unexpired) nonces."""
        async with self._lock:
            return len(self._store)
