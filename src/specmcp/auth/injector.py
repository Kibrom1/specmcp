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

Architecture (Phase 1 refactor):
  The flat isinstance chain in _inject_scheme has been replaced with an
  internal _AuthSchemeHandler protocol and a lookup dict (_HANDLERS).
  This sets up clean extension points for Phase 4 (Authorization Code)
  without changing the public inject() API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from specmcp.auth.token_cache import CachedToken, TokenCache
from specmcp.config import (
    ApiKeyAuthConfig,
    AuthSchemeConfig,
    BearerAuthConfig,
    Config,
    OAuth2ClientCredentialsConfig,
    SensitiveStr,
    _resolve_value_from,
)
from specmcp.core.model import AuthRequirement
from specmcp.errors import AuthConfigError, TokenRefreshError

# SessionContext imported lazily to avoid circular imports at module load.
# The type annotation uses a string forward reference.
# from specmcp.runtime.session import SessionContext  (imported below)


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


# ---------------------------------------------------------------------------
# Internal handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _AuthSchemeHandler(Protocol):
    """Internal protocol: one handler per auth scheme type.

    Replaces the isinstance chain in the original _inject_scheme method.
    Handlers mutate *headers* or *params* in place. Async because OAuth
    handlers may need to fetch tokens.
    """

    async def apply(
        self,
        resolved: ResolvedScheme,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_caches: dict[str, TokenCache],
        fetch_token_fn: Any,
        session: Any,  # SessionContext | None
    ) -> None: ...


class _ApiKeyHandler:
    async def apply(
        self,
        resolved: ResolvedScheme,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_caches: dict[str, TokenCache],
        fetch_token_fn: Any,
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
    async def apply(
        self,
        resolved: ResolvedScheme,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_caches: dict[str, TokenCache],
        fetch_token_fn: Any,
        session: Any,
    ) -> None:
        # Phase 2: client token passed in MCP session init takes priority
        if session is not None and session.client_token is not None:
            headers["Authorization"] = f"Bearer {session.client_token.reveal()}"
            return
        assert resolved.credential is not None
        headers["Authorization"] = f"Bearer {resolved.credential.reveal()}"


class _ClientCredentialsHandler:
    async def apply(
        self,
        resolved: ResolvedScheme,
        headers: dict[str, str],
        params: dict[str, str],
        *,
        token_caches: dict[str, TokenCache],
        fetch_token_fn: Any,
        session: Any,
    ) -> None:
        cache = token_caches[resolved.scheme_name]
        token = await cache.get_or_refresh(
            lambda r=resolved, c=resolved.config: fetch_token_fn(r, c)
        )
        headers["Authorization"] = f"Bearer {token}"


_HANDLERS: dict[type, _AuthSchemeHandler] = {
    ApiKeyAuthConfig: _ApiKeyHandler(),
    BearerAuthConfig: _BearerHandler(),
    OAuth2ClientCredentialsConfig: _ClientCredentialsHandler(),
}


# ---------------------------------------------------------------------------
# AuthInjector
# ---------------------------------------------------------------------------


@dataclass
class AuthInjector:
    """Injects auth credentials into outbound HTTP request parameters.

    Usage::

        injector = AuthInjector.build(config)
        headers, params = await injector.inject(op.auth, headers={}, params={}, session=session)

    ``inject`` mutates *copies* of the passed dicts and returns them.
    The originals are never modified.
    """

    _schemes: dict[str, ResolvedScheme] = field(default_factory=dict, repr=False)
    _token_caches: dict[str, TokenCache] = field(default_factory=dict, repr=False)

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
        # OAuth2 client_id/secret are also resolved eagerly so missing env
        # vars fail at startup, not on the first tool call.
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
                    credential=static_values[name],
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
            session: Optional SessionContext for per-user token lookup.
                None for v1 configs (no behaviour change).

        Returns:
            Tuple of (merged_headers, merged_params) with auth added.

        Raises:
            AuthConfigError: if no configured group satisfies the
                operation's auth requirements.
            TokenRefreshError: if an OAuth token fetch fails.
            AuthRequiredError: if a session has no token and the user
                must authenticate (Phase 4 Authorization Code flow).
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
        session: Any,
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
        session: Any,
    ) -> None:
        """Dispatch to the appropriate handler for the scheme type."""
        cfg = resolved.config
        handler = _HANDLERS.get(type(cfg))
        if handler is None:
            raise AuthConfigError(
                f"Auth scheme '{resolved.scheme_name}' has unsupported type "
                f"{type(cfg).__name__!r}. This is an internal error."
            )
        await handler.apply(
            resolved,
            headers,
            params,
            token_caches=self._token_caches,
            fetch_token_fn=self._fetch_token,
            session=session,
        )

    async def _fetch_token(
        self,
        resolved: ResolvedScheme,
        cfg: OAuth2ClientCredentialsConfig,
    ) -> CachedToken:
        """Exchange client credentials for an OAuth access token.

        Opens a dedicated httpx.AsyncClient rather than reusing the main
        HttpClient. This is intentional: the auth layer must not be coupled
        to the runtime layer, and token fetches are infrequent (once per
        expiry window, typically 1 hour). trust_env=False matches the main
        HttpClient policy.

        Security: the client_secret and access_token must never appear in
        any exception message. Only token_url and status_code are safe.

        RFC 6749 §2.3.1: client credentials are sent via HTTP Basic Auth, NOT
        in the request body. This prevents credential exposure in proxy logs
        and server access logs.
        """
        assert resolved.oauth_client_id is not None
        assert resolved.oauth_client_secret is not None

        client_id = resolved.oauth_client_id.reveal()
        client_secret = resolved.oauth_client_secret.reveal()

        # Body contains only grant_type, optional scope, and any extra_params.
        # client_id and client_secret are NOT in the body (RFC 6749 §2.3.1).
        form: dict[str, str] = {
            "grant_type": "client_credentials",
            **cfg.extra_params,
        }
        if cfg.scopes:
            form["scope"] = " ".join(cfg.scopes)

        async with httpx.AsyncClient(trust_env=False) as client:
            try:
                response = await client.post(
                    cfg.token_url,
                    auth=(client_id, client_secret),  # HTTP Basic Auth — RFC 6749 §2.3.1
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
            access_token=body["access_token"],  # never log; used only in Authorization header
            expires_at=time.monotonic() + expires_in,
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
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def configured_schemes(self) -> frozenset[str]:
        """Names of all configured auth schemes."""
        return frozenset(self._schemes)

    def has_scheme(self, name: str) -> bool:
        """Return True if *name* is configured."""
        return name in self._schemes
