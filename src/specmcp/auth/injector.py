"""
specmcp Auth Injector.

Resolves credentials at startup and injects them into outbound HTTP
requests based on the operation's auth requirements.

Design rules:
  - SensitiveStr.reveal() is the *only* place real values escape.
  - No credential value ever appears in an exception message or log.
  - If an operation requires a scheme that is not configured, we raise
    AuthConfigError at dispatch time (not startup) so that unconfigured
    schemes only block the operations that actually need them.
  - The injector is built once and held for the server lifetime.
  - inject() is async because OAuth token fetches are network calls.
    For non-OAuth schemes inject() completes synchronously (no await inside).

Architecture:
  The flat isinstance chain in _inject_scheme has been replaced with an
  internal _AuthSchemeHandler protocol and a _HANDLERS lookup dict.
  This sets up clean extension points for Phase 4 (Authorization Code)
  without changing the public inject() API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from specmcp.auth.oauth2_authcode import AuthCodeHandler
from specmcp.auth.token_cache import CachedToken, TokenCache
from specmcp.config import (
    ApiKeyAuthConfig,
    AuthSchemeConfig,
    BearerAuthConfig,
    Config,
    OAuth2AuthorizationCodeConfig,
    OAuth2ClientCredentialsConfig,
    SensitiveStr,
    _resolve_value_from,
)
from specmcp.core.model import AuthRequirement
from specmcp.errors import AuthConfigError, TokenRefreshError


# ---------------------------------------------------------------------------
# Internal handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _AuthSchemeHandler(Protocol):
    """Internal protocol for scheme-specific injection logic."""

    async def apply(
        self,
        resolved: "ResolvedScheme",
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_cache: "TokenCache | None",
        session: Any,  # SessionContext | None — typed as Any to avoid circular import
    ) -> None:
        """Mutate *headers*/*params* in place."""
        ...


class _ApiKeyHandler:
    async def apply(
        self,
        resolved: "ResolvedScheme",
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_cache: "TokenCache | None",
        session: Any,
    ) -> None:
        cfg = resolved.config
        assert isinstance(cfg, ApiKeyAuthConfig)
        assert resolved.credential is not None
        value = resolved.credential.reveal()
        if cfg.in_ == "header":
            headers[cfg.name] = value
        elif cfg.in_ == "query":
            params[cfg.name] = value
        elif cfg.in_ == "cookie":
            existing = headers.get("Cookie", "")
            cookie_pair = f"{cfg.name}={value}"
            headers["Cookie"] = f"{existing}; {cookie_pair}" if existing else cookie_pair


class _BearerHandler:
    """Bearer token handler.

    Priority order:
      1. ``session.client_token`` — bearer token forwarded by the MCP client
         via ``initialize._meta.bearer_token``. The client is responsible for
         keeping it fresh; specmcp never refreshes it.
      2. Static env-var token from the config ``bearer`` scheme.
    """

    async def apply(
        self,
        resolved: "ResolvedScheme",
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_cache: "TokenCache | None",
        session: Any,
    ) -> None:
        # Priority 1: client-supplied bearer token (from MCP initialize._meta)
        if session is not None and session.client_token is not None:
            headers["Authorization"] = f"Bearer {session.client_token.reveal()}"
            return

        # Priority 2: static env-var bearer token
        assert resolved.credential is not None
        headers["Authorization"] = f"Bearer {resolved.credential.reveal()}"


class _ClientCredentialsHandler:
    """OAuth 2.0 client_credentials handler with token caching.

    Uses HTTP Basic Auth (RFC 6749 §2.3.1) to send client credentials:
    the client_id and client_secret are sent as the ``auth=`` parameter
    to httpx, which encodes them as an Authorization: Basic header.
    Credentials are NEVER sent in the request body.
    """

    async def apply(
        self,
        resolved: "ResolvedScheme",
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_cache: "TokenCache | None",
        session: Any,
    ) -> None:
        assert token_cache is not None
        token = await token_cache.get_or_refresh(
            lambda r=resolved, c=resolved.config: _fetch_client_credentials_token(r, c)  # type: ignore[arg-type]
        )
        headers["Authorization"] = f"Bearer {token}"


# Registry: config type → handler instance
_HANDLERS: dict[type, _AuthSchemeHandler] = {
    ApiKeyAuthConfig: _ApiKeyHandler(),
    BearerAuthConfig: _BearerHandler(),
    OAuth2ClientCredentialsConfig: _ClientCredentialsHandler(),
}


# ---------------------------------------------------------------------------
# ResolvedScheme
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedScheme:
    """A single auth scheme with its credential(s) already resolved from env.

    For apiKey / bearer schemes: ``credential`` holds the static token.
    For OAuth2 schemes: ``oauth_client_id`` and ``oauth_client_secret`` hold
    the client credentials; the access token is managed by TokenCache and
    never stored here.
    """

    scheme_name: str
    config: AuthSchemeConfig
    credential: SensitiveStr | None = None          # apiKey / bearer
    oauth_client_id: SensitiveStr | None = None     # oauth2_client_credentials
    oauth_client_secret: SensitiveStr | None = None  # oauth2_client_credentials


@dataclass
class AuthInjector:
    """Injects auth credentials into outbound HTTP request parameters.

    Usage::

        injector = AuthInjector.build(config)
        headers, params = await injector.inject(op.auth, headers={}, params={})

    ``inject`` mutates *copies* of the passed dicts and returns them.
    The originals are never modified.

    Auth code schemes are registered separately via
    ``register_auth_code_handler()`` because they require runtime state
    (token store, nonce store) that is not available at config-parse time.
    """

    _schemes: dict[str, ResolvedScheme] = field(default_factory=dict, repr=False)
    _token_caches: dict[str, TokenCache] = field(default_factory=dict, repr=False)
    _auth_code_handlers: dict[str, AuthCodeHandler] = field(
        default_factory=dict, repr=False
    )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, config: Config | None) -> "AuthInjector":
        """Resolve credentials from *config* and return a ready injector.

        For apiKey and bearer schemes: credentials are resolved from env vars
        immediately (fails fast if any env var is missing).

        For oauth2_client_credentials schemes: client_id and client_secret are
        resolved from env vars now; the access token is fetched lazily on the
        first inject() call and cached thereafter.

        If *config* is None (e.g. ``--spec`` mode with no config file),
        an injector with no schemes is returned. Operations that need auth
        will raise ``AuthConfigError`` at dispatch time.
        """
        if config is None:
            return cls(_schemes={}, _token_caches={})

        # Resolve static credentials (apiKey, bearer) eagerly.
        static_values = config.resolve_auth_values()

        schemes: dict[str, ResolvedScheme] = {}
        token_caches: dict[str, TokenCache] = {}

        for name, scheme_cfg in config._auth_schemes.items():  # noqa: SLF001
            if isinstance(scheme_cfg, OAuth2ClientCredentialsConfig):
                client_id = _resolve_value_from(scheme_cfg.client_id_from, name)
                client_secret = _resolve_value_from(scheme_cfg.client_secret_from, name)
                schemes[name] = ResolvedScheme(
                    scheme_name=name,
                    config=scheme_cfg,
                    oauth_client_id=client_id,
                    oauth_client_secret=client_secret,
                )
                token_caches[name] = TokenCache()
            else:
                schemes[name] = ResolvedScheme(
                    scheme_name=name,
                    config=scheme_cfg,
                    credential=static_values.get(name),
                )

        return cls(_schemes=schemes, _token_caches=token_caches)

    # ------------------------------------------------------------------
    # Injection
    # ------------------------------------------------------------------

    async def inject(
        self,
        auth_requirements: list[list[AuthRequirement]],
        *,
        headers: dict[str, str],
        params: dict[str, str],
        session: Any = None,  # SessionContext | None — typed as Any to avoid circular import
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return (headers, params) with auth credentials injected.

        OpenAPI security is expressed as a list of *OR* groups, each
        group being an *AND* list of schemes. We pick the first group
        for which every scheme is configured.

        Args:
            auth_requirements: ``op.auth`` from the Operation model.
                Empty list means no auth required.
            headers: Existing request headers dict (will be copied).
            params: Existing query params dict (will be copied).
            session: Optional SessionContext. When present, client_token
                takes priority over the static env-var bearer token.

        Returns:
            Tuple of (merged_headers, merged_params) with auth added.

        Raises:
            AuthConfigError: if no configured group satisfies the
                operation's auth requirements.
            TokenRefreshError: if an OAuth token fetch fails.
        """
        if not auth_requirements:
            return dict(headers), dict(params)

        for group in auth_requirements:
            if self._group_is_satisfied(group):
                return await self._apply_group(group, headers=headers, params=params, session=session)

        missing = self._find_missing_schemes(auth_requirements)
        raise AuthConfigError(
            f"Operation requires auth schemes that are not configured: "
            f"{sorted(missing)}. "
            f"Add them to the 'auth:' section of your mcp.config.yaml."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _group_is_satisfied(self, group: list[AuthRequirement]) -> bool:
        return all(r.scheme_name in self._schemes for r in group)

    async def _apply_group(
        self,
        group: list[AuthRequirement],
        *,
        headers: dict[str, str],
        params: dict[str, str],
        session: Any = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Apply each scheme in *group* to copies of headers/params."""
        out_headers = dict(headers)
        out_params = dict(params)

        for req in group:
            resolved = self._schemes[req.scheme_name]
            await self._inject_scheme(resolved, out_headers, out_params, session=session)

        return out_headers, out_params

    async def _inject_scheme(
        self,
        resolved: ResolvedScheme,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        session: Any = None,
    ) -> None:
        """Dispatch to the appropriate handler based on config type."""
        # Auth code schemes use dedicated per-scheme handler instances
        if isinstance(resolved.config, OAuth2AuthorizationCodeConfig):
            auth_code_handler = self._auth_code_handlers.get(resolved.scheme_name)
            if auth_code_handler is None:
                raise AuthConfigError(
                    f"Auth scheme '{resolved.scheme_name}' (oauth2_authorization_code) "
                    "has no registered AuthCodeHandler. Call "
                    "injector.register_auth_code_handler() at startup."
                )
            await auth_code_handler.apply(headers, params, session=session)
            return

        handler = _HANDLERS.get(type(resolved.config))
        if handler is None:
            raise AuthConfigError(
                f"Auth scheme '{resolved.scheme_name}' has unsupported type "
                f"{type(resolved.config).__name__!r}. This is an internal error."
            )
        token_cache = self._token_caches.get(resolved.scheme_name)
        await handler.apply(
            resolved,
            headers,
            params,
            token_cache=token_cache,
            session=session,
        )

    def _find_missing_schemes(
        self, auth_requirements: list[list[AuthRequirement]]
    ) -> set[str]:
        """Collect all scheme names referenced but not configured."""
        missing: set[str] = set()
        for group in auth_requirements:
            for req in group:
                if req.scheme_name not in self._schemes:
                    missing.add(req.scheme_name)
        return missing

    # ------------------------------------------------------------------
    # Auth code handler registration
    # ------------------------------------------------------------------

    def register_auth_code_handler(
        self, scheme_name: str, handler: AuthCodeHandler
    ) -> None:
        """Register an OAuth2 Authorization Code handler for *scheme_name*.

        Must be called after ``build()`` and before the first ``inject()``
        that involves this scheme.  The *handler* instance encapsulates the
        token store, nonce issuer, and login base URL — all runtime state
        that is not available at config-parse time.

        The corresponding scheme is also added to ``_schemes`` as a
        placeholder ``ResolvedScheme`` (with no credential) so that
        ``_group_is_satisfied`` recognises it as configured.
        """
        self._auth_code_handlers[scheme_name] = handler
        # Register a placeholder ResolvedScheme so _group_is_satisfied works
        if scheme_name not in self._schemes:
            from specmcp.config import OAuth2AuthorizationCodeConfig
            # We can't reconstruct the config here, so use a minimal sentinel
            # value; _inject_scheme dispatches via _auth_code_handlers first.
            self._schemes[scheme_name] = ResolvedScheme(
                scheme_name=scheme_name,
                config=handler._config,  # noqa: SLF001
            )

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def configured_schemes(self) -> frozenset[str]:
        """Names of all configured auth schemes."""
        return frozenset(self._schemes) | frozenset(self._auth_code_handlers)

    def has_scheme(self, name: str) -> bool:
        """Return True if *name* is configured."""
        return name in self._schemes or name in self._auth_code_handlers

    def invalidate_cached_tokens(
        self, auth_requirements: list[list[AuthRequirement]]
    ) -> bool:
        """Invalidate any cached OAuth access tokens referenced by *auth_requirements*.

        Called by the dispatcher after receiving a 401 from the upstream so
        that the next ``inject()`` call fetches a fresh token.

        Only ``oauth2_client_credentials`` schemes have a ``TokenCache`` and
        are invalidated here.  ``oauth2_authorization_code`` tokens are stored
        in a ``TokenStore`` and are not touched — a 401 on an auth-code scheme
        typically means the token was revoked server-side; the user must
        re-authenticate via the login URL.

        Returns:
            ``True`` if at least one cache was invalidated (a retry may
            succeed).  ``False`` if no cached token was found (the 401 is
            unrelated to token caching; re-raising is appropriate).
        """
        invalidated = False
        for group in auth_requirements:
            for req in group:
                cache = self._token_caches.get(req.scheme_name)
                if cache is not None:
                    cache.invalidate()
                    invalidated = True
        return invalidated


# ---------------------------------------------------------------------------
# OAuth client_credentials token fetch (module-level to keep handler thin)
# ---------------------------------------------------------------------------


async def _fetch_client_credentials_token(
    resolved: ResolvedScheme,
    cfg: OAuth2ClientCredentialsConfig,
) -> CachedToken:
    """Exchange client credentials for an OAuth access token.

    Uses HTTP Basic Auth (RFC 6749 §2.3.1) — credentials are sent in the
    Authorization header, NOT in the request body. This fixes security
    finding H4 from the design review.

    Security: the client_secret and access_token must never appear in
    any exception message. Only token_url and status_code are safe.
    """
    assert resolved.oauth_client_id is not None
    assert resolved.oauth_client_secret is not None

    client_id = resolved.oauth_client_id.reveal()
    client_secret = resolved.oauth_client_secret.reveal()

    # Build form body: grant_type + scopes + extra_params only (no credentials)
    form: dict[str, str] = {"grant_type": "client_credentials", **cfg.extra_params}
    if cfg.scopes:
        form["scope"] = " ".join(cfg.scopes)

    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            response = await client.post(
                cfg.token_url,
                auth=(client_id, client_secret),  # HTTP Basic Auth per RFC 6749 §2.3.1
                data=form,
                timeout=15.0,
            )
        except httpx.RequestError as exc:
            raise TokenRefreshError(
                f"Failed to reach OAuth token endpoint {cfg.token_url!r}: {type(exc).__name__}"
            ) from exc

    if response.status_code != 200:
        raise TokenRefreshError(
            f"OAuth token endpoint {cfg.token_url!r} returned HTTP {response.status_code}",
            status_code=response.status_code,
        )

    try:
        body: dict[str, Any] = response.json()
    except Exception as exc:
        raise TokenRefreshError(
            f"OAuth token endpoint {cfg.token_url!r} returned non-JSON response"
        ) from exc

    if "access_token" not in body:
        raise TokenRefreshError(
            f"OAuth token endpoint {cfg.token_url!r} response missing 'access_token' field"
        )

    expires_in = float(body.get("expires_in", 3600))
    return CachedToken(
        access_token=SensitiveStr(body["access_token"]),
        expires_at=time.monotonic() + expires_in,
    )
