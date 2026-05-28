"""OAuth Authorization Code HTTP endpoints for specmcp.

Provides four Starlette routes mounted under /auth:

  GET  /auth/login?nonce=<token>
       Validate and consume a single-use nonce issued by the auth layer.
       Generate a PKCE verifier + challenge, build the authorization URL,
       and redirect the user's browser to the upstream authorization server.

  GET  /auth/callback?code=<code>&state=<state>
       Receive the authorization code redirect from the upstream server.
       Validate the state (CSRF protection), retrieve the PKCE verifier,
       exchange the code for tokens via HTTP Basic Auth, store tokens in
       the TokenStore, and show a success page to the user.

  GET  /auth/status?session=<session_id>
       Return whether the session has a valid (non-expired) token.
       Designed for polling by the MCP client after the user logs in.

  DELETE /auth/session/<session_id>
       Revoke and delete tokens for a session (logout).
       Restricted to loopback callers by default; requires a management
       bearer token when management.bind is "all".

All HTML responses include SECURITY_HEADERS to prevent clickjacking,
MIME sniffing, and information leakage.

Usage — mount routes onto a Starlette app::

    from specmcp.runtime.oauth_handler import OAuthHandlerState, build_oauth_routes

    state = OAuthHandlerState(...)
    routes = build_oauth_routes(state)
    app = Starlette(routes=mcp_routes + routes)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from cachetools import TTLCache
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from specmcp.auth.login_nonce import LoginNonceStore
from specmcp.auth.pkce import generate_challenge, generate_verifier
from specmcp.auth.state import make_state, verify_state
from specmcp.auth.token_store import OAuthTokens, TokenStore
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr, _resolve_value_from

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security headers — applied to all HTML responses
# ---------------------------------------------------------------------------

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# ---------------------------------------------------------------------------
# Resolved scheme (pre-resolved credentials, one per configured scheme)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedAuthCodeScheme:
    """A single OAuth2 Authorization Code scheme with credentials resolved.

    Created at startup by resolving env-var references in the config.
    """

    scheme_name: str
    config: OAuth2AuthorizationCodeConfig
    client_id: SensitiveStr
    client_secret: SensitiveStr | None  # None for public clients
    token_store: TokenStore

    @classmethod
    def build(
        cls,
        scheme_name: str,
        cfg: OAuth2AuthorizationCodeConfig,
        token_store: TokenStore,
    ) -> "ResolvedAuthCodeScheme":
        """Resolve credentials from env vars and return a ready scheme."""
        client_id = _resolve_value_from(cfg.client_id_from, scheme_name)
        # client_secret_from is required in the model (not Optional).
        # Public-client support (where it would be omitted) is future work.
        client_secret = _resolve_value_from(cfg.client_secret_from, scheme_name)
        return cls(
            scheme_name=scheme_name,
            config=cfg,
            client_id=client_id,
            client_secret=client_secret,
            token_store=token_store,
        )


# ---------------------------------------------------------------------------
# OAuthHandlerState — shared state for all OAuth route handlers
# ---------------------------------------------------------------------------


@dataclass
class OAuthHandlerState:
    """Shared state for the OAuth HTTP endpoint handlers.

    Holds all inter-request state: nonce stores, PKCE store, resolved
    scheme credentials, and the server secret for state signing.

    server_secret is used to sign/verify the ``state`` parameter (CSRF).
    It must be a stable secret per server run (not per-request). If not
    provided, a random one is generated at instantiation — but note that
    this means in-flight flows survive only for the process lifetime.
    """

    schemes: dict[str, ResolvedAuthCodeScheme]
    """Maps scheme_name → ResolvedAuthCodeScheme."""

    nonce_store: LoginNonceStore = field(default_factory=LoginNonceStore)
    """Single-use nonce → session_id (5-minute TTL)."""

    server_secret: str = field(default_factory=lambda: __import__("secrets").token_hex(32))
    """HMAC secret for state signing/verification."""

    management_token: SensitiveStr | None = None
    """Optional management bearer token for DELETE /auth/session/<id>."""

    management_bind_all: bool = False
    """If True, DELETE is accessible from any IP (requires management_token)."""

    # Internal PKCE store: state → verifier (TTLCache, 10 min TTL)
    _pkce_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _pkce_store: TTLCache = field(
        default_factory=lambda: TTLCache(maxsize=10_000, ttl=600),
        repr=False,
    )

    # Internal nonce → scheme_name cache (same 5-min TTL as nonce_store)
    _scheme_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _nonce_scheme: TTLCache = field(
        default_factory=lambda: TTLCache(maxsize=10_000, ttl=300),
        repr=False,
    )

    # ----------------------------------------------------------------
    # Nonce issue/consume (combines session_id + scheme_name in one op)
    # ----------------------------------------------------------------

    async def issue_nonce(self, session_id: str, scheme_name: str) -> str:
        """Issue a nonce for *session_id* + *scheme_name* and return it."""
        nonce = await self.nonce_store.issue(session_id)
        async with self._scheme_lock:
            self._nonce_scheme[nonce] = scheme_name
        return nonce

    async def consume_nonce(self, nonce: str) -> tuple[str, str] | None:
        """Consume *nonce* and return (session_id, scheme_name), or None if invalid."""
        session_id = await self.nonce_store.consume(nonce)
        if session_id is None:
            return None
        async with self._scheme_lock:
            scheme_name = self._nonce_scheme.pop(nonce, None)
        if scheme_name is None:
            return None
        return session_id, scheme_name

    # ----------------------------------------------------------------
    # PKCE store
    # ----------------------------------------------------------------

    async def store_verifier(self, state: str, verifier: str, scheme_name: str) -> None:
        """Store a PKCE verifier and its scheme_name keyed by *state*.

        Both values are stored together so the callback handler can determine
        which scheme to use for token exchange without a separate lookup.
        """
        async with self._pkce_lock:
            self._pkce_store[state] = (verifier, scheme_name)

    async def consume_verifier(self, state: str) -> tuple[str, str] | None:
        """Pop and return ``(verifier, scheme_name)`` for *state*, or None."""
        async with self._pkce_lock:
            return self._pkce_store.pop(state, None)


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------


def _html_success(message: str = "Authentication successful") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authentication successful</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 600px;
           margin: 80px auto; padding: 0 20px; color: #333; }}
    .ok {{ color: #2e7d32; font-size: 1.4em; margin-bottom: 0.5em; }}
    p {{ color: #555; }}
  </style>
</head>
<body>
  <div class="ok">&#10003; {message}</div>
  <p>You can close this window and return to your application.</p>
</body>
</html>"""


def _html_error(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authentication error</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 600px;
           margin: 80px auto; padding: 0 20px; color: #333; }}
    .err {{ color: #c62828; font-size: 1.4em; margin-bottom: 0.5em; }}
    p {{ color: #555; }}
  </style>
</head>
<body>
  <div class="err">&#10007; Authentication error</div>
  <p>{message}</p>
  <p>Please close this window and try the login link again.</p>
</body>
</html>"""


def _secure_html(content: str, status_code: int = 200) -> HTMLResponse:
    """Return an HTMLResponse with all security headers applied."""
    return HTMLResponse(content=content, status_code=status_code, headers=SECURITY_HEADERS)


def _secure_error(message: str, status_code: int = 400) -> HTMLResponse:
    return _secure_html(_html_error(message), status_code=status_code)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_login(request: Request, state_obj: OAuthHandlerState) -> Response:
    """GET /auth/login?nonce=<token>

    1. Consume nonce → (session_id, scheme_name)
    2. Generate PKCE verifier + challenge
    3. Build signed state token
    4. Store verifier in pkce_store[state]
    5. Redirect to upstream authorization URL
    """

    nonce = request.query_params.get("nonce", "").strip()
    if not nonce:
        logger.warning("login_nonce_invalid: reason=missing")
        return _secure_error("Missing login nonce.", status_code=400)

    result = await state_obj.consume_nonce(nonce)
    if result is None:
        logger.warning("login_nonce_invalid: reason=expired_or_unknown")
        return _secure_error(
            "The login link has expired or already been used. "
            "Please retry your request to get a new link.",
            status_code=400,
        )

    session_id, scheme_name = result
    resolved = state_obj.schemes.get(scheme_name)
    if resolved is None:
        logger.error("login_scheme_not_found: scheme=%s", scheme_name)
        return _secure_error("Auth scheme configuration error.", status_code=500)

    cfg = resolved.config
    verifier = generate_verifier()
    challenge = generate_challenge(verifier)
    signed_state = make_state(session_id, state_obj.server_secret)

    await state_obj.store_verifier(signed_state, verifier, scheme_name)

    # Build authorization URL
    import urllib.parse

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": resolved.client_id.reveal(),
        "redirect_uri": cfg.redirect_uri,
        "state": signed_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **cfg.extra_params,
    }
    if cfg.scopes:
        params["scope"] = " ".join(cfg.scopes)

    auth_url = cfg.authorization_url + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=auth_url, status_code=302)


async def _handle_callback(request: Request, state_obj: OAuthHandlerState) -> Response:
    """GET /auth/callback?code=<code>&state=<state>

    1. Check for upstream error param — render error page if present
    2. Validate state (CSRF) → session_id
    3. Consume PKCE verifier (replay protection)
    4. Exchange code for tokens via HTTP Basic Auth
    5. Validate returned scope (log downgrade)
    6. Store tokens, render success page
    """

    # Step 1: upstream error
    error_code = request.query_params.get("error", "").strip()
    if error_code:
        # Log error code only — error_description may contain PII
        logger.warning("oauth_callback_error: error=%s", error_code)
        # HTML-escape before inserting into page — defense-in-depth against
        # reflected XSS even though the CSP already blocks script execution.
        import html as _html_mod
        safe_error = _html_mod.escape(error_code)
        # Return 200 with an HTML error page — the browser already landed here
        # from the upstream redirect; a 4xx would confuse some OAuth flows.
        return _secure_html(_html_error(f"Authorization failed: {safe_error}"))

    raw_state = request.query_params.get("state", "").strip()
    code = request.query_params.get("code", "").strip()

    if not raw_state or not code:
        logger.warning("oauth_callback_error: error=missing_params")
        return _secure_error("Missing code or state parameter.", status_code=400)

    # Step 2: verify state (CSRF)
    try:
        session_id = verify_state(raw_state, state_obj.server_secret)
    except ValueError as exc:
        logger.warning("oauth_state_mismatch: reason=%s", exc)
        return _secure_error("State verification failed. Please try logging in again.")

    # Step 3: consume PKCE verifier (single-use) — also recovers scheme_name
    pkce_entry = await state_obj.consume_verifier(raw_state)
    if pkce_entry is None:
        logger.warning("oauth_state_mismatch: reason=verifier_expired_or_replayed")
        return _secure_error("Login session expired. Please try logging in again.")
    verifier, scheme_name = pkce_entry

    # Look up scheme — guaranteed by store_verifier recording the scheme_name
    resolved = state_obj.schemes.get(scheme_name)
    if resolved is None:
        logger.error("oauth_callback_scheme_not_found: scheme=%s", scheme_name)
        return _secure_error("OAuth scheme configuration error.", status_code=500)

    cfg = resolved.config

    # Step 4: exchange code for tokens
    try:
        tokens = await _exchange_code(cfg, resolved, code, verifier)
    except Exception as exc:
        logger.error("oauth_token_exchange_failed: error=%s", exc)
        return _secure_error("Token exchange failed. Please try logging in again.")

    # Step 5: scope validation
    _check_scope_downgrade(cfg, tokens, session_id=session_id)

    # Step 6: store tokens
    try:
        await resolved.token_store.save(session_id, tokens)
    except Exception as exc:
        logger.error("token_store_error: session=%s error=%s", session_id, exc)
        return _secure_error("Failed to save authentication tokens.", status_code=500)

    logger.info("oauth_tokens_stored: session=%s scheme=%s", session_id, resolved.scheme_name)
    return _secure_html(_html_success("You are now authenticated."))


async def _handle_status(request: Request, state_obj: OAuthHandlerState) -> Response:
    """GET /auth/status?session=<session_id>

    Returns JSON: {"authenticated": true|false}.
    Designed for polling by the MCP client after redirecting the user.
    """
    session_id = request.query_params.get("session", "").strip()

    if not session_id:
        return JSONResponse({"authenticated": False, "error": "missing session"}, status_code=400)

    # Check any configured scheme for this session
    for resolved in state_obj.schemes.values():
        tokens = await resolved.token_store.get(session_id)
        if tokens is not None and not tokens.is_expired():
            return JSONResponse({"authenticated": True})

    return JSONResponse({"authenticated": False})


async def _handle_delete_session(request: Request, state_obj: OAuthHandlerState) -> Response:
    """DELETE /auth/session/<session_id>

    Revoke and delete tokens. Protected by loopback binding or
    management bearer token.
    """
    # Access control
    if not _check_management_access(request, state_obj):
        return Response(status_code=403)

    session_id = request.path_params.get("session_id", "").strip()
    if not session_id:
        return Response(status_code=400)

    # Only delete if the session actually has tokens
    deleted_any = False
    for resolved in state_obj.schemes.values():
        try:
            existing = await resolved.token_store.get(session_id)
            if existing is not None:
                await resolved.token_store.delete(session_id)
                deleted_any = True
        except Exception:
            pass

    if deleted_any:
        return Response(status_code=204)
    return Response(status_code=404)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_management_access(request: Request, state_obj: OAuthHandlerState) -> bool:
    """Return True if the request is authorised to use the management endpoint."""
    if not state_obj.management_bind_all:
        # Loopback-only mode: check client IP.
        # Include ::ffff:127.0.0.1 for IPv4-mapped IPv6 on dual-stack Linux.
        client_host = getattr(request.client, "host", "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
            return False
        return True

    # bind: all — require bearer token
    if state_obj.management_token is None:
        return False  # management_token_from must be set when bind=all

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[7:].strip()
    import hmac as _hmac
    expected = state_obj.management_token.reveal()
    return _hmac.compare_digest(provided.encode(), expected.encode())


async def _exchange_code(
    cfg: OAuth2AuthorizationCodeConfig,
    resolved: ResolvedAuthCodeScheme,
    code: str,
    verifier: str,
) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    Uses HTTP Basic Auth (RFC 6749 §2.3.1): client_id/secret go in the
    Authorization header, NOT in the POST body.
    """
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": verifier,
        **cfg.extra_params,
    }

    client_id = resolved.client_id.reveal()
    auth_arg: Any
    if resolved.client_secret is not None:
        auth_arg = (client_id, resolved.client_secret.reveal())
    else:
        # Public client: send client_id in body per RFC 6749 §4.1.3
        form["client_id"] = client_id
        auth_arg = None

    async with httpx.AsyncClient(trust_env=False) as client:
        kwargs: dict[str, Any] = {"data": form, "timeout": 15.0}
        if auth_arg is not None:
            kwargs["auth"] = auth_arg

        response = await client.post(cfg.token_url, **kwargs)

    if response.status_code != 200:
        raise RuntimeError(
            f"Token endpoint {cfg.token_url!r} returned HTTP {response.status_code}"
        )

    try:
        body: dict[str, Any] = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Token endpoint {cfg.token_url!r} returned non-JSON response"
        ) from exc

    if "access_token" not in body:
        raise RuntimeError(
            f"Token endpoint {cfg.token_url!r} response missing 'access_token'"
        )

    return OAuthTokens.from_token_response(body)


def _check_scope_downgrade(
    cfg: OAuth2AuthorizationCodeConfig,
    tokens: OAuthTokens,
    *,
    session_id: str,
) -> None:
    """Log a warning if the server granted fewer scopes than requested."""
    if not cfg.scopes or tokens.scope is None:
        return

    granted = set(tokens.scope.split())
    requested = set(cfg.scopes)
    missing = requested - granted
    if missing:
        logger.warning(
            "scope_downgrade_detected: session=%s requested=%s granted=%s missing=%s",
            session_id, sorted(requested), sorted(granted), sorted(missing),
        )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_oauth_routes(state: OAuthHandlerState) -> list[Route]:
    """Return a list of Starlette Routes for the OAuth endpoints.

    The *state* is captured in closures, so no ``app.state`` attachment is needed.
    Mount these alongside the MCP SSE routes in the Starlette app::

        from starlette.applications import Starlette
        from starlette.routing import Route
        from specmcp.runtime.oauth_handler import build_oauth_routes

        app = Starlette(routes=[
            Route("/sse", mcp_sse_handler),
            Route("/messages", mcp_message_handler, methods=["POST"]),
            *build_oauth_routes(oauth_state),
        ])
    """
    async def login(request: Request) -> Response:
        return await _handle_login(request, state)

    async def callback(request: Request) -> Response:
        return await _handle_callback(request, state)

    async def status(request: Request) -> Response:
        return await _handle_status(request, state)

    async def delete_session(request: Request) -> Response:
        return await _handle_delete_session(request, state)

    return [
        Route("/auth/login", endpoint=login, methods=["GET"]),
        Route("/auth/callback", endpoint=callback, methods=["GET"]),
        Route("/auth/status", endpoint=status, methods=["GET"]),
        Route("/auth/session/{session_id}", endpoint=delete_session, methods=["DELETE"]),
    ]


def mount_oauth_state(app: Any, state: OAuthHandlerState) -> None:
    """Attach *state* to *app.state* for introspection or testing.

    Note: route handlers use closures and do not require this call.
    Kept for compatibility and test convenience.
    """
    app.state.oauth = state
