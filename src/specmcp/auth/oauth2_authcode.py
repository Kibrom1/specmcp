"""OAuth 2.0 Authorization Code + PKCE token handler for the AuthInjector.

This module implements the auth injector handler for the
``oauth2_authorization_code`` scheme type. It:

1. Checks the token store for a valid (non-expired) access token for the
   current session. If found, injects it as a Bearer header.
2. If the access token is expired but a refresh token exists, attempts a
   silent token refresh using HTTP Basic Auth. On success, stores the new
   tokens and injects the new access token.
3. If no valid token can be obtained, issues a single-use login nonce and
   raises ``AuthRequiredError`` with the URL the user must visit.

Design decisions:
  - Per-session asyncio.Lock prevents concurrent in-flight requests from
    racing to refresh the same token (double-refresh / write-write race).
  - Nonce issuance is only attempted once per AuthRequiredError — the
    login URL is single-use and must be forwarded to the user immediately.
  - ``login_base_url`` is the origin of the specmcp OAuth HTTP server
    (e.g. ``http://localhost:8765``). The full login URL becomes
    ``{login_base_url}/auth/login?nonce={nonce}``.
  - ``issue_nonce`` is an async callable matching
    ``OAuthHandlerState.issue_nonce(session_id, scheme_name) -> str``.
    Injected at startup so this module stays free of circular imports.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx
from cachetools import TTLCache

from specmcp.auth.token_store import OAuthTokens, TokenStore
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr
from specmcp.errors import AuthRequiredError, TokenRefreshError

logger = logging.getLogger(__name__)

# How long to keep per-session refresh locks alive after last use (seconds).
_LOCK_TTL = 3600


class AuthCodeHandler:
    """Per-scheme handler for OAuth2 Authorization Code + PKCE flows.

    One instance is created per configured ``oauth2_authorization_code``
    scheme at startup and registered with the ``AuthInjector``.

    Args:
        scheme_name:    Name of the auth scheme (matches the key in config.auth).
        config:         The parsed OAuth2AuthorizationCodeConfig.
        client_id:      Resolved client ID.
        client_secret:  Resolved client secret, or None for public clients.
        token_store:    Token store for this scheme (shared with the OAuth
                        HTTP endpoints so tokens written by /auth/callback
                        are visible here).
        issue_nonce:    Async callable ``(session_id, scheme_name) -> nonce``
                        — typically ``OAuthHandlerState.issue_nonce``.
        login_base_url: Origin of the specmcp OAuth server
                        (e.g. ``"http://localhost:8765"``).

    Usage::

        handler = AuthCodeHandler(
            scheme_name="myAuth",
            config=cfg,
            client_id=SensitiveStr(client_id),
            client_secret=SensitiveStr(client_secret),
            token_store=token_store,
            issue_nonce=oauth_state.issue_nonce,
            login_base_url="http://localhost:8765",
        )
        injector.register_auth_code_handler("myAuth", handler)
    """

    def __init__(
        self,
        scheme_name: str,
        config: OAuth2AuthorizationCodeConfig,
        client_id: SensitiveStr,
        client_secret: SensitiveStr | None,
        token_store: TokenStore,
        issue_nonce: Callable[[str, str], Awaitable[str]],
        login_base_url: str,
    ) -> None:
        self._scheme_name = scheme_name
        self._config = config
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_store = token_store
        self._issue_nonce = issue_nonce
        self._login_base_url = login_base_url.rstrip("/")

        # Per-session refresh locks — lazily created, expire 1h after last use
        self._lock_cache: TTLCache = TTLCache(maxsize=10_000, ttl=_LOCK_TTL)
        self._cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        session: Any,  # SessionContext | None
    ) -> None:
        """Inject an access token header or raise ``AuthRequiredError``.

        Mutates *headers* in place on success.

        Args:
            headers: Outbound request headers dict.
            params:  Outbound query params dict (unused for Bearer auth;
                     provided for API symmetry).
            session: SessionContext for this request. Must have a
                     non-empty ``session_id`` attribute.

        Raises:
            AuthConfigError:   If session is None / missing session_id.
            AuthRequiredError: If no valid token can be obtained. The
                               ``login_url`` attribute holds the URL the
                               user must visit to authenticate.
        """
        from specmcp.errors import AuthConfigError

        if session is None or not getattr(session, "session_id", None):
            raise AuthConfigError(
                f"Auth scheme '{self._scheme_name}' (oauth2_authorization_code) "
                "requires an active session. "
                "This scheme is only available over the HTTP/SSE transport."
            )

        session_id: str = session.session_id
        lock = await self._get_session_lock(session_id)

        async with lock:
            tokens = await self._token_store.get(session_id)

            # Fast path: valid, non-expired token
            if tokens is not None and not tokens.is_expired():
                headers["Authorization"] = f"Bearer {tokens.access_token.reveal()}"
                return

            # Slow path: attempt silent refresh if refresh token present
            if tokens is not None and tokens.refresh_token is not None:
                try:
                    new_tokens = await self._do_refresh(tokens)
                    await self._token_store.save(session_id, new_tokens)
                    headers["Authorization"] = f"Bearer {new_tokens.access_token.reveal()}"
                    logger.info(
                        "oauth_token_refreshed: session=%s scheme=%s",
                        session_id,
                        self._scheme_name,
                    )
                    return
                except TokenRefreshError as exc:
                    logger.warning(
                        "oauth_refresh_failed: session=%s scheme=%s error=%s",
                        session_id,
                        self._scheme_name,
                        exc,
                    )
                    # Fall through: refresh failed, user must re-authenticate

        # No valid token available — issue a login nonce and raise
        login_url = await self._build_login_url(session_id)
        raise AuthRequiredError(
            f"Authentication required for scheme '{self._scheme_name}'. "
            "Please ask the user to log in.",
            session_id=session_id,
            login_url=login_url,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session asyncio.Lock, creating it lazily."""
        async with self._cache_lock:
            lock = self._lock_cache.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._lock_cache[session_id] = lock
            return lock

    async def _do_refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        """Exchange a refresh token for a new access (and possibly refresh) token.

        Uses HTTP Basic Auth per RFC 6749 §6. The refresh_token value
        itself is never logged or included in exception messages.

        Args:
            tokens: The current (expired) tokens; must have a refresh_token.

        Returns:
            New OAuthTokens with a fresh access_token. The refresh_token is
            preserved from the response if provided, otherwise carried over
            from *tokens*.

        Raises:
            TokenRefreshError: On any network, HTTP, or protocol error.
        """
        assert tokens.refresh_token is not None

        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token.reveal(),
        }
        if self._config.scopes:
            form["scope"] = " ".join(self._config.scopes)

        client_id = self._client_id.reveal()
        auth_arg: Any
        if self._client_secret is not None:
            auth_arg = (client_id, self._client_secret.reveal())
        else:
            # Public client: send client_id in form body (RFC 6749 §4.1.3)
            form["client_id"] = client_id
            auth_arg = None

        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                kwargs: dict[str, Any] = {"data": form, "timeout": 15.0}
                if auth_arg is not None:
                    kwargs["auth"] = auth_arg
                response = await http.post(self._config.token_url, **kwargs)
        except httpx.RequestError as exc:
            raise TokenRefreshError(
                f"Failed to reach token endpoint {self._config.token_url!r}: "
                f"{type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise TokenRefreshError(
                f"Token endpoint {self._config.token_url!r} returned "
                f"HTTP {response.status_code} during refresh",
                status_code=response.status_code,
            )

        try:
            body: dict[str, Any] = response.json()
        except Exception as exc:
            raise TokenRefreshError(
                f"Token endpoint {self._config.token_url!r} returned non-JSON response"
            ) from exc

        if "access_token" not in body:
            raise TokenRefreshError(
                f"Token endpoint {self._config.token_url!r} missing 'access_token' field"
            )

        return OAuthTokens.from_token_response(
            body,
            existing_refresh_token=tokens.refresh_token,
        )

    async def _build_login_url(self, session_id: str) -> str | None:
        """Issue a single-use nonce and return the full login URL."""
        try:
            nonce = await self._issue_nonce(session_id, self._scheme_name)
            return f"{self._login_base_url}/auth/login?nonce={nonce}"
        except Exception as exc:
            logger.error(
                "oauth_nonce_issue_failed: session=%s scheme=%s error=%s",
                session_id,
                self._scheme_name,
                exc,
            )
            return None
