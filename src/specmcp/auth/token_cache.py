"""
specmcp OAuth 2.0 token cache.

Stores a single access token per scheme and refreshes it before expiry.
The asyncio.Lock prevents thundering-herd refreshes when multiple concurrent
tool calls race on an expired token — only one coroutine fetches a new token
while the others wait and then reuse it.

Design constraints:
  - The access_token is a plain str, not SensitiveStr. It lives only in
    CachedToken inside TokenCache inside AuthInjector — never in ResolvedScheme
    or any field that could be serialised or logged. All TokenRefreshError
    messages must omit the token value (only token_url and status_code are safe).
  - No persistence to disk in v1.1 — in-memory only.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class CachedToken:
    """A fetched access token with its expiry timestamp."""

    access_token: str    # never log
    expires_at: float    # monotonic clock timestamp

    def is_expired(self, buffer_seconds: float = 60.0) -> bool:
        """Return True if the token will expire within *buffer_seconds*.

        The 60-second buffer ensures a token is refreshed before the upstream
        rejects it, even under clock drift or network latency to the token
        endpoint.
        """
        return time.monotonic() >= self.expires_at - buffer_seconds


@dataclass
class TokenCache:
    """Per-scheme OAuth token cache with async refresh lock.

    Usage::

        cache = TokenCache()
        token = await cache.get_or_refresh(my_refresh_coroutine)

    The *refresh_fn* is called at most once per expiry window even when
    multiple tool calls race simultaneously on the same scheme.
    """

    _token: CachedToken | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def get_or_refresh(
        self,
        refresh_fn: Callable[[], Awaitable[CachedToken]],
    ) -> str:
        """Return a valid access token, fetching a new one if necessary.

        Args:
            refresh_fn: An async callable with no arguments that returns a
                fresh ``CachedToken``. Called under the lock, so it will not
                be invoked concurrently for the same scheme.

        Returns:
            The raw access token string. Caller is responsible for not logging it.

        Raises:
            TokenRefreshError: if *refresh_fn* raises (propagated unchanged).
        """
        async with self._lock:
            if self._token is None or self._token.is_expired():
                self._token = await refresh_fn()
            return self._token.access_token

    def invalidate(self) -> None:
        """Force the next call to get_or_refresh() to fetch a new token.

        Useful for handling 401 responses from the upstream: call invalidate()
        and retry the request once to get a fresh token.
        Not currently called automatically — reserved for v1.2 retry logic.
        """
        self._token = None
