# OAuth 2.0 Design — specmcp v2

**Status:** Draft v0.2 — security review incorporated  
**Scope:** User-dependent tokens, Authorization Code + PKCE, Client Credentials, token refresh, session lifecycle  
**Upstream docs:** `api-to-mcp-requirements.md` v0.3, `api-to-mcp-design-v1.md` v0.4, `implementation-plan-v1.md`  
**Branch:** `feat/oauth`  
**Security review:** 2026-05-23 — all findings resolved in this revision

---

## 1. Problem Statement

v1 supports a single shared credential per auth scheme, loaded from an environment variable at startup. This works for server-to-server integrations and single-user personal deployments, but breaks for any scenario where different users have different tokens:

- SaaS product where each customer has their own API key or OAuth token
- Multi-user MCP deployment (e.g. a team sharing one specmcp instance)
- APIs that issue short-lived user-scoped access tokens via OAuth 2.0

This document designs the v2 auth layer that lifts this constraint.

---

## 2. Design Principles

1. **v1 must keep working unchanged.** The `apiKey` and `bearer` config types and their env-var resolution are not touched. Users on v1 configs upgrade to v2 binary with zero changes.
2. **The `AuthScheme` protocol is the only interface.** All auth — v1 static, v2 client credentials, v2 authorization code — flows through `apply()` and `handle_response()`. No auth logic in the dispatcher.
3. **Secrets never leave the server.** Tokens are stored in the token store and injected at dispatch time. The LLM never sees a credential, and credentials never appear in logs.
4. **Fail closed.** If a session has no token and no way to get one, the tool call returns `"Authentication required"` — not a partial or unauthenticated request.
5. **One retry per call, no exceptions.** Refresh-and-retry on 401 is capped at one attempt per tool call, regardless of the auth scheme. Side effects of repeated retries are not acceptable.

---

## 3. The Two Flows

### 3.1 Client Credentials (server-to-server)

`OAuth2ClientCredentialsConfig` is already stubbed in `config.py`. The server holds a `client_id` and `client_secret`, exchanges them for an access token at startup, caches it in memory, and refreshes it before expiry. **No user interaction required.** This is suitable for APIs where specmcp itself is the authenticated party, not individual users.

```
specmcp starts
     │
     ▼
POST /oauth/token  (credentials via HTTP Basic Auth — see §9.4)
  grant_type=client_credentials, scope=...
     │
     ▼
{ access_token, expires_in }
     │
     ▼
Cached in OAuth2TokenCache
Refreshed automatically before expiry
```

All tool calls in all sessions use the same token. This is behaviorally equivalent to v1 `bearer` except the token is auto-refreshed.

### 3.2 Authorization Code + PKCE (user-dependent)

Each user authenticates via their own browser. specmcp acts as the OAuth client, not the resource owner. After the user logs in, specmcp holds their per-session access token and refresh token.

```
LLM calls a tool (user has no token yet)
     │
     ▼
specmcp issues a single-use login nonce (§9.2), returns to LLM:
  "Authentication required. Visit: https://<specmcp-host>/auth/login?nonce=<one-time-token>"
     │
     ▼
User opens URL in browser (nonce expires after 5 minutes, single-use)
     │
     ▼
/auth/login validates nonce → resolves session_id → discards nonce
     │
     ▼
specmcp redirects to upstream authorization server
  with: response_type=code, client_id, redirect_uri, scope, state, code_challenge (PKCE S256)
     │
     ▼
User logs in at upstream authorization server
     │
     ▼
Upstream redirects back to specmcp /auth/callback?code=...&state=...
  (or /auth/callback?error=...&error_description=... — see §10)
     │
     ▼
specmcp validates state, exchanges code for tokens (credentials via HTTP Basic Auth):
  POST /oauth/token
    grant_type=authorization_code, code, redirect_uri, code_verifier (PKCE)
     │
     ▼
{ access_token, refresh_token, expires_in, scope }
     │
     ▼
Validate returned scope against config.scopes (§9.3)
Store in TokenStore keyed by session_id
     │
     ▼
LLM retries the tool call — now succeeds with injected Bearer token
```

---

## 4. New Components

### 4.1 SessionContext (`src/specmcp/runtime/session.py`)

Carries per-session state through the dispatch pipeline. Created when an MCP session opens; destroyed when it closes.

```python
from dataclasses import dataclass, field
from specmcp.config import SensitiveStr

@dataclass
class SessionContext:
    session_id: str

    # Token passed by MCP client in initialize.meta (Level 1 / v1.1 feature).
    # Takes priority over token_store lookup when present.
    # Note: client tokens have no server-managed refresh path. The MCP client
    # is responsible for keeping them fresh. When the token expires, the LLM
    # will receive a 401-derived error until the client provides a new token
    # via a fresh initialize. Document this limitation in the v2 user guide.
    client_token: SensitiveStr | None = None

    # Per-session metadata from the MCP initialize request.
    metadata: dict = field(default_factory=dict)
```

**What SessionContext does NOT hold:** the actual OAuth tokens. Those live in the `TokenStore`, keyed by `session_id`. `SessionContext` is a lightweight per-call object that tells the auth layer where to look.

### 4.2 TokenStore (`src/specmcp/auth/token_store.py`)

Abstract persistent store for OAuth tokens, keyed by session ID.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from specmcp.config import SensitiveStr

@dataclass
class OAuthTokens:
    access_token: SensitiveStr
    refresh_token: SensitiveStr | None  # updated on every successful refresh (§9.1)
    expires_at: float                   # Unix timestamp; 0.0 = server did not supply expires_in
    token_type: str = "Bearer"
    scope: str = ""                     # scope as returned by the authorization server

class TokenStore(ABC):
    @abstractmethod
    async def get(self, session_id: str) -> OAuthTokens | None: ...

    @abstractmethod
    async def save(self, session_id: str, tokens: OAuthTokens) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...

    @abstractmethod
    async def all_sessions(self) -> list[str]: ...
```

**Two implementations:**

| Class | Storage | Survives restart | Use case |
|---|---|---|---|
| `InMemoryTokenStore` | Python dict | No | Dev, single-session, testing |
| `SqliteTokenStore` | SQLite file | Yes | Production multi-user |

Config selects the store:

```yaml
# mcp.config.yaml
auth:
  myOAuth:
    type: oauth2_authorization_code
    ...
    token_store: sqlite          # or: memory (default)
    token_store_path: ~/.specmcp/tokens.db   # only for sqlite
```

`SensitiveStr` values are encrypted at rest in the SQLite store using a key derived from a `TOKEN_STORE_KEY` env var (see §9.5 for key rotation procedure). If the key is missing, specmcp refuses to start with `oauth2_authorization_code` configured.

### 4.3 OAuth2AuthScheme (`src/specmcp/auth/oauth2.py`)

Implements the `AuthScheme` protocol for both flows.

```python
import asyncio
from collections import defaultdict

# Per-session refresh locks — prevents concurrent refresh race condition (§9.1).
# One lock per session_id; created lazily.
_refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class OAuth2AuthScheme:
    """Handles both client_credentials and authorization_code flows."""

    async def apply(
        self,
        request: httpx.Request,
        session: SessionContext | None = None,
    ) -> httpx.Request:
        token = await self._get_token(session)
        if token is None:
            raise AuthRequiredError(
                session_id=session.session_id if session else None,
                login_nonce=await self._issue_login_nonce(session),  # single-use (§9.2)
            )
        request.headers["Authorization"] = f"Bearer {token.access_token.reveal()}"
        return request

    async def handle_response(
        self,
        request: httpx.Request,
        response: httpx.Response,
        session: SessionContext | None = None,
    ) -> Literal["accept", "retry"]:
        if response.status_code != 401 or session is None:
            return "accept"
        # Attempt one token refresh under session lock (prevents concurrent refresh race)
        refreshed = await self._refresh(session)
        return "retry" if refreshed else "accept"

    async def _get_token(self, session: SessionContext | None) -> OAuthTokens | None:
        # 1. Client token passed directly in MCP session init (Level 1)
        #    No proactive refresh — client manages expiry. See SessionContext docstring.
        if session and session.client_token:
            return OAuthTokens(
                access_token=session.client_token,
                refresh_token=None,
                expires_at=0.0,
            )
        # 2. Look up in token store
        if session:
            tokens = await self._store.get(session.session_id)
            if tokens and not self._is_expired(tokens):
                return tokens
            # Proactive refresh: token exists but is expiring within buffer window
            if tokens and tokens.refresh_token and self._is_expired(tokens):
                async with _refresh_locks[session.session_id]:
                    # Re-read under lock — another coroutine may have already refreshed
                    tokens = await self._store.get(session.session_id)
                    if tokens and not self._is_expired(tokens):
                        return tokens  # already refreshed by concurrent caller
                    if tokens and tokens.refresh_token:
                        refreshed = await self._do_refresh(session, tokens)
                        if refreshed:
                            return await self._store.get(session.session_id)
        # 3. Client credentials: use shared token (auto-refreshed)
        if self._flow == "client_credentials":
            return await self._get_or_refresh_cc_token()
        return None

    async def _refresh(self, session: SessionContext) -> bool:
        """Acquire per-session lock before refreshing. Returns True if refresh succeeded."""
        async with _refresh_locks[session.session_id]:
            tokens = await self._store.get(session.session_id)
            if not tokens or not tokens.refresh_token:
                return False
            # Re-check under lock — may have already been refreshed concurrently
            if not self._is_expired(tokens):
                return True
            return await self._do_refresh(session, tokens)

    async def _do_refresh(self, session: SessionContext, tokens: OAuthTokens) -> bool:
        """
        Exchange refresh token for new access token.

        IMPORTANT: The token endpoint returns a NEW refresh token (RFC 6749 §6,
        plus RFC 7009 token rotation). We MUST store the new refresh_token value.
        Storing the old one will cause invalid_grant on the next refresh.
        """
        try:
            response = await self._token_client.post(
                self._config.token_url,
                # Credentials sent via HTTP Basic Auth, not request body (§9.4)
                auth=(self._config.client_id, self._config.client_secret.reveal()),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token.reveal(),
                },
            )
            response.raise_for_status()
            data = response.json()
            new_tokens = OAuthTokens(
                access_token=SensitiveStr(data["access_token"]),
                # Use the NEW refresh_token if the server returned one; fall back to
                # the existing token only for servers that don't rotate (non-standard).
                refresh_token=SensitiveStr(data["refresh_token"])
                    if "refresh_token" in data
                    else tokens.refresh_token,
                expires_at=time.time() + data["expires_in"] if "expires_in" in data else 0.0,
                scope=data.get("scope", tokens.scope),
            )
            await self._store.save(session.session_id, new_tokens)
            return True
        except Exception:
            structlog.get_logger().warning("auth_refresh_failed", session_id=session.session_id)
            return False

    def _is_expired(self, tokens: OAuthTokens, buffer_seconds: float = 60.0) -> bool:
        """Return True if the token expires within buffer_seconds.
        Returns False when expires_at == 0.0 (expiry unknown — server omitted expires_in).
        """
        if tokens.expires_at == 0.0:
            return False  # Unknown expiry — rely on 401 fallback path
        return time.time() >= tokens.expires_at - buffer_seconds
```

### 4.4 Login Nonce Store (`src/specmcp/auth/login_nonce.py`)

Single-use, time-limited tokens that map to session IDs. These are what appear in the login URL surfaced to the LLM — the session_id itself is never exposed (see §9.2).

```python
from cachetools import TTLCache
import secrets

# Max 10,000 pending login flows; each expires after 5 minutes.
# Bounded size prevents memory exhaustion under flood of /auth/login requests.
_nonce_store: TTLCache[str, str] = TTLCache(maxsize=10_000, ttl=300)
_store_lock = asyncio.Lock()

async def issue_nonce(session_id: str) -> str:
    nonce = secrets.token_urlsafe(32)  # 256 bits of entropy
    async with _store_lock:
        _nonce_store[nonce] = session_id
    return nonce

async def consume_nonce(nonce: str) -> str | None:
    """Returns session_id and removes the nonce (single-use). Returns None if expired/invalid."""
    async with _store_lock:
        return _nonce_store.pop(nonce, None)
```

### 4.5 OAuth HTTP Endpoints (`src/specmcp/runtime/oauth_handler.py`)

Required only when `oauth2_authorization_code` is configured. Runs as routes on the HTTP transport (stdio cannot handle browser redirects).

All HTML responses include the following security headers:
```
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

| Route | Purpose |
|---|---|
| `GET /auth/login?nonce=<token>` | Validate and consume nonce → resolve session_id; generate PKCE verifier + challenge; build authorization URL; redirect user to upstream |
| `GET /auth/callback?code=...&state=...` | Validate state; handle `?error=...` responses; exchange code for tokens; validate returned scope; store in TokenStore; show success page |
| `GET /auth/status?session=<id>` | Return whether the session has a valid token (for polling) |
| `DELETE /auth/session/<id>` | Revoke and delete tokens for a session (logout) — restricted to loopback or operator-authenticated management interface; see §9.6 |

**OAuth callback error handling:** If the upstream authorization server returns an error (e.g. `?error=access_denied`), the callback handler renders a user-facing error page. The `error` code is logged as `oauth_callback_error`. The `error_description` parameter (which may contain PII) is **not** logged. No token exchange is attempted.

```python
# In the callback handler:
if "error" in request.query_params:
    error_code = request.query_params["error"]  # e.g. "access_denied"
    logger.warning("oauth_callback_error", error=error_code, session_id=session_id)
    return render_error_page(f"Authorization failed: {error_code}")
```

The `state` parameter is a signed, time-limited token (see §9 for the exact encoding). This prevents CSRF and open-redirect attacks.

PKCE `code_verifier` is stored temporarily in memory using a `TTLCache` keyed by `state` (max 10,000 entries, 10-minute TTL — see §9 for details). It is never persisted and never logged.

---

## 5. Updated AuthScheme Protocol

The `apply()` and `handle_response()` signatures gain an optional `session` parameter. All v1 schemes (`ApiKeyAuth`, `BearerAuth`) ignore it — **no behaviour change for v1 configs**.

```python
class AuthScheme(Protocol):
    async def apply(
        self,
        request: httpx.Request,
        session: SessionContext | None = None,  # NEW — ignored by v1 schemes
    ) -> httpx.Request: ...

    async def handle_response(
        self,
        request: httpx.Request,
        response: httpx.Response,
        session: SessionContext | None = None,  # NEW — ignored by v1 schemes
    ) -> Literal["accept", "retry"]: ...
```

---

## 6. Dispatcher Changes

The dispatcher loop gains a `session` parameter and passes it through to `AuthScheme`:

```
tools/call(name, args)  ←  carried on MCP session object
   │
   ▼
1. Lookup       — find SimplifiedOperation by tool name
2. Validate     — args against llm_input_schema
3. Map          — args + ArgumentMap → DispatchRequest
4. Authenticate — AuthScheme.apply(request, session=session)   ← session added
5. Send         — httpx.AsyncClient.send()
6. Receive      — handle AuthRequiredError → return login-nonce message to LLM
7. Receive      — handle response; if 401: AuthScheme.handle_response(session=session)
8. Retry once   — if handle_response returns "retry"
9. Map result   — HTTP response → MCP tool result
10. Log + return
```

The `AuthRequiredError` path (step 6) is new. When the user has not authenticated yet, instead of a generic error, the LLM receives an actionable message:

```
Authentication required. Please ask the user to visit the following URL to log in:
https://<specmcp-host>/auth/login?nonce=<one-time-token>

The link expires in 5 minutes. After logging in, retry your request.
```

The nonce is single-use (consumed on first visit to `/auth/login`) and expires after 5 minutes. It does not reveal the session_id. The session_id never appears in the LLM's context window.

The structured log event `auth_required` is emitted at step 6. The nonce is not included in the log event.

---

## 7. Config Schema (version "2")

New auth scheme types. The `version` key bumps to `"2"` to signal the new capabilities; v1 configs (`version: "1"`) continue to work without changes.

```yaml
version: "2"

auth:
  # --- Already in v1, unchanged ---
  myApiKey:
    type: apiKey
    in: header
    name: X-Api-Key
    value_from: env(MY_API_KEY)

  # --- Already stubbed in config.py, completing implementation ---
  myClientCreds:
    type: oauth2_client_credentials
    token_url: https://auth.example.com/oauth/token  # must be https://
    client_id_from: env(MY_CLIENT_ID)
    client_secret_from: env(MY_CLIENT_SECRET)
    scopes: [read, write]
    extra_params: {}           # forwarded to token endpoint; reserved OAuth fields are rejected

  # --- New in v2 ---
  myUserOAuth:
    type: oauth2_authorization_code
    authorization_url: https://auth.example.com/oauth/authorize  # must be https://
    token_url: https://auth.example.com/oauth/token              # must be https://
    client_id_from: env(MY_CLIENT_ID)
    client_secret_from: env(MY_CLIENT_SECRET)   # optional (public clients omit this)
    scopes: [openid, read, write]
    redirect_uri: http://localhost:8765/auth/callback
    token_store: sqlite                          # memory | sqlite
    token_store_path: env(TOKEN_STORE_PATH)      # default: ~/.specmcp/tokens.db
    # encryption key for tokens at rest (required for sqlite store)
    token_store_key_from: env(TOKEN_STORE_KEY)
    extra_params: {}
```

**Config validation rules (enforced at startup):**

- `authorization_url` and `token_url` must use `https://` scheme. Exception: `http://localhost` and `http://127.0.0.1` are permitted for local development.
- `extra_params` must not contain any of these reserved fields: `grant_type`, `code`, `code_verifier`, `redirect_uri`, `client_id`, `client_secret`, `response_type`. A `ConfigError` is raised at startup if any reserved key is present.
- `TOKEN_STORE_KEY` must be present (via env) when `token_store: sqlite` is configured.
- `SERVER_SECRET` must be present (via env) when `oauth2_authorization_code` is configured.

**Config version compatibility:**

| Config version | v1 binary | v2 binary |
|---|---|---|
| `"1"` | ✅ | ✅ (loads, v1 auth behaviour) |
| `"2"` | ❌ (rejects with clear error) | ✅ |

---

## 8. Session Lifecycle

```
MCP client connects (initialize)
   │
   ▼
SessionContext created: session_id = random UUID (generated by specmcp)
   │
   ├── client_token from initialize.meta? → store in SessionContext
   │   NOTE: client_token expiry is the MCP client's responsibility.
   │   When it expires, LLM receives upstream 401 errors. No server-side refresh.
   └── no client_token → TokenStore lookup on first tool call
   │
   ▼
Tool calls execute (session_id flows through dispatcher)
   │
   ├── Token found and valid → inject, dispatch
   ├── Token expiring, refresh_token present → acquire session lock → refresh → dispatch
   ├── No token → issue single-use nonce → return AuthRequiredError with login URL
   └── 401 from upstream → handle_response → acquire session lock → one refresh attempt
   │
   ▼
MCP client disconnects
   │
   ├── InMemoryTokenStore: tokens discarded
   └── SqliteTokenStore: tokens retained (persist across restarts)
```

**Session reconnect:** `SqliteTokenStore` entries are keyed by `session_id`. Reconnect with the same session_id reuses the stored token without re-authentication. Since session IDs are random UUIDs generated by specmcp on each `initialize`, the MCP client must persist and replay its session_id to benefit from this. This should be documented as an explicit opt-in in the MCP client configuration guide, not an implicit behaviour users will discover on their own.

**Session cleanup:** `SessionContext` objects are removed from the serve layer's session map on disconnect. Token store cleanup for abandoned sessions (e.g. client disconnected mid-auth flow) is handled by a background task that removes token store entries older than a configurable `session_ttl_days` (default: 30).

---

## 9. Security Considerations

### 9.1 Refresh token rotation

Modern authorization servers (Google, GitHub, Auth0, Okta) invalidate the refresh token after it is used and return a new one in the token endpoint response. `_do_refresh()` **must** store the new `refresh_token` from the response. If the server did not return a new refresh token (non-standard behaviour), the existing value is retained as a fallback, but this is not guaranteed to work — document this caveat in the operator guide.

A per-session `asyncio.Lock` (`_refresh_locks[session_id]`) prevents concurrent refresh operations from sending the same refresh token twice. All code paths that call `_do_refresh()` must acquire this lock first. The lock is checked by re-reading the token store under lock before exchanging, so a second concurrent caller skips the exchange if the first already succeeded.

### 9.2 Login URL — session ID isolation

The session_id is never included in the login URL surfaced to the LLM. Instead, a single-use nonce (256-bit random, 5-minute TTL) is issued and included in the URL. The `LoginNonceStore` (§4.4) maps nonce → session_id server-side. When the user visits `/auth/login?nonce=<token>`, the nonce is consumed (deleted from the store) and cannot be replayed.

This ensures that even if the LLM provider logs conversation history, the login URL is useless after it is visited once or after 5 minutes.

### 9.3 Scope validation

After the authorization code exchange, the returned `scope` field is compared against the scopes in `config.scopes`. If the authorization server returns a narrower scope (RFC 6749 §5.1 permits this), a structured `scope_downgrade_detected` event is logged and the operator is notified at startup rather than silently accepting reduced permissions. Tool calls that would require downgraded scopes will fail with 403 responses from upstream; the log event helps operators diagnose why.

### 9.4 Client credential transmission

Client credentials are sent to the token endpoint via **HTTP Basic Authentication** (`Authorization: Basic base64(client_id:client_secret)`), following RFC 6749 §2.3.1. Sending credentials in the request body (`client_id=...&client_secret=...`) is explicitly avoided to reduce exposure via proxy logs and server access logs.

For authorization servers that require body-form credentials (non-compliant but common with some legacy servers), an opt-in config field will be added: `token_endpoint_auth_method: basic | post` (Phase 5).

### 9.5 PKCE

Authorization Code flow always uses PKCE with the `S256` code challenge method. The `plain` method is not supported and will be rejected at startup if specified.

The PKCE `code_verifier` is generated as:
```python
import secrets, base64
code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
# 43 characters, 256 bits of entropy — meets RFC 7636 §4.1
```

The `code_challenge` is `base64url(SHA-256(code_verifier))`.

Verifiers are stored in a `TTLCache(maxsize=10_000, ttl=600)` keyed by the `state` value. The `maxsize=10_000` bound prevents memory exhaustion if an attacker floods `/auth/login` — requests beyond the cache limit cause the oldest entries to be evicted (safe: the affected login flows will fail at callback validation and the user will retry). Cleanup is automatic — `cachetools.TTLCache` handles expiry without a background task.

### 9.6 State parameter

The `state` value is computed as:

```python
import hmac, hashlib, struct, time

def make_state(session_id: str, secret: bytes) -> str:
    timestamp = int(time.time())
    # Fixed-width timestamp (8 bytes big-endian) eliminates length-extension ambiguity.
    # session_id is a UUID so its length is fixed (36 chars), but we use explicit
    # length-prefixed encoding for correctness regardless of session_id format.
    msg = struct.pack(">I", len(session_id)) + session_id.encode() + struct.pack(">Q", timestamp)
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{session_id}.{timestamp}.{sig}"

def verify_state(state: str, secret: bytes, ttl_seconds: int = 600) -> str | None:
    """Returns session_id if state is valid and unexpired. Returns None otherwise."""
    try:
        session_id, ts_str, sig = state.split(".", 2)
        timestamp = int(ts_str)
    except ValueError:
        return None
    if time.time() - timestamp > ttl_seconds:
        return None
    expected = make_state_sig(session_id, timestamp, secret)
    if not hmac.compare_digest(sig, expected):
        return None
    return session_id
```

`SERVER_SECRET` is a required env var when `oauth2_authorization_code` is configured. Rotating it invalidates all in-flight OAuth flows (users mid-login will see a state-mismatch error and must click the login link again). This UX impact is low and is documented in the operator runbook.

### 9.7 Token storage encryption

SQLite tokens are encrypted using AES-256-GCM with a key derived via HKDF from `TOKEN_STORE_KEY`. The nonce is stored alongside the ciphertext.

**Key rotation procedure:**
1. Set `TOKEN_STORE_KEY_NEW` env var to the new key value.
2. Run `specmcp token-store rotate --old-key env(TOKEN_STORE_KEY) --new-key env(TOKEN_STORE_KEY_NEW)`. This script reads each record, decrypts with the old key, re-encrypts with the new key, and writes atomically.
3. Update the environment to set `TOKEN_STORE_KEY=<new-value>` and remove `TOKEN_STORE_KEY_NEW`.
4. Restart specmcp.

If the operator rotates the key without running the migration script, existing tokens become unreadable and sessions will require re-authentication — this is safe (fail-closed) but will force all users to log in again. Document this clearly in the operator guide.

### 9.8 Redirect URI validation

The `redirect_uri` in config must exactly match what the upstream authorization server has registered. specmcp does not allow dynamic redirect URIs.

### 9.9 Token values in logs

`OAuthTokens.access_token` and `.refresh_token` are `SensitiveStr`. The existing `structlog` redaction already handles this — no additional changes needed.

### 9.10 Management endpoint security (`DELETE /auth/session/<id>`)

The `DELETE /auth/session/<id>` endpoint allows revocation of a session's tokens. Without access control, any caller who knows a session_id can forcibly log out a user. This endpoint must be protected by one of:

- **Loopback binding (default):** only reachable from `127.0.0.1` / `::1`. Suitable for single-host deployments.
- **Operator token (production):** an `Authorization: Bearer <MANAGEMENT_TOKEN>` header check, where `MANAGEMENT_TOKEN` is a configurable env var. Required for multi-host or containerised deployments.

Config:
```yaml
management:
  bind: loopback          # loopback | all
  management_token_from: env(SPECMCP_MANAGEMENT_TOKEN)   # required if bind: all
```

---

## 10. Failure Modes and Handling

| Failure | User (LLM) sees | Operator log event |
|---|---|---|
| No token for session | `"Authentication required. Visit: <nonce-url>"` | `auth_required` |
| Token expired, no refresh token | `"Authentication required. Visit: <nonce-url>"` | `auth_required` |
| Token expired, refresh fails (upstream revoked) | `"Authentication required. Visit: <nonce-url>"` | `auth_refresh_failed` |
| Concurrent refresh — second caller re-uses fresh token | Transparent (second caller uses refreshed token) | — |
| Client credentials exchange fails at startup | Hard startup error; server exits | `cc_token_exchange_failed` |
| State mismatch on OAuth callback | 400 error page shown to user | `oauth_state_mismatch` |
| OAuth callback with error response | User-facing error page (error code only, no description) | `oauth_callback_error` |
| Login nonce expired or already used | 400 error page; user clicks login link again | `login_nonce_invalid` |
| Scope downgrade detected after exchange | Tokens stored; operator notified at startup | `scope_downgrade_detected` |
| Token store unavailable (SQLite locked) | `"Internal error (request_id: ...)"` | `token_store_error` |
| `TOKEN_STORE_KEY` missing at startup | Hard startup error; server exits | N/A |
| `SERVER_SECRET` missing at startup | Hard startup error; server exits | N/A |
| `extra_params` contains reserved OAuth field | Hard startup error; server exits | N/A |
| `authorization_url` / `token_url` uses `http://` | Hard startup error (unless localhost) | N/A |

---

## 11. What `specmcp init` Does for OAuth Specs

When the upstream spec declares `oauth2` security schemes, `specmcp init` now generates:

```yaml
auth:
  myOAuth:
    type: oauth2_authorization_code        # or oauth2_client_credentials
    authorization_url: <detected from spec>
    token_url: <detected from spec>
    client_id_from: env(MYOAUTH_CLIENT_ID)
    client_secret_from: env(MYOAUTH_CLIENT_SECRET)
    scopes: [<detected from spec>]
    redirect_uri: http://localhost:8765/auth/callback
    token_store: memory                    # safe default; operator upgrades to sqlite
    token_store_key_from: env(MYOAUTH_TOKEN_STORE_KEY)
```

And adds to `.env.example`:
```
MYOAUTH_CLIENT_ID=your-client-id
MYOAUTH_CLIENT_SECRET=your-client-secret
MYOAUTH_TOKEN_STORE_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
SERVER_SECRET=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
```

---

## 12. Implementation Order

Follow the same principle as v1: build the infrastructure first, then layer features on top. All work on `feat/oauth` branch. Full v1 test suite must stay green on every commit.

### Phase 1 — Plumbing (2–3 days, no behaviour change)

- `src/specmcp/runtime/session.py` — `SessionContext` dataclass
- `src/specmcp/auth/token_store.py` — `TokenStore` ABC + `InMemoryTokenStore`
- `src/specmcp/auth/login_nonce.py` — `LoginNonceStore` (TTLCache, issue/consume)
- Update `AuthScheme.apply()` and `handle_response()` signatures (optional `session` param)
- Update dispatcher to create `SessionContext` per call, pass to `AuthScheme` (always `None` for now)
- Unit tests: `SessionContext` construction; `InMemoryTokenStore` CRUD; `LoginNonceStore` issue/consume/expiry; dispatcher passes session through

### Phase 2 — Level 1: client token via session init (1–2 days)

- Update MCP `initialize` handler to read `meta.bearer_token` → store in `SessionContext`
- Update `BearerAuth.apply()` to prefer `session.client_token` over env var
- Integration test: client passes token in `initialize.meta` → tool call uses it

### Phase 3 — Client Credentials (3–4 days)

- `src/specmcp/auth/oauth2.py` — `OAuth2ClientCredentialsScheme`
  - Token exchange at startup via HTTP Basic Auth, cached in `InMemoryTokenStore` (single session key `"__cc__"`)
  - Proactive refresh: check expiry before each call, refresh if within 60s of expiry
  - `handle_response` returns `"retry"` on 401 → one forced refresh attempt under lock
- `SqliteTokenStore` — AES-256-GCM field-level encryption, key rotation migration script
- Config: `oauth2_client_credentials` type fully wired; config validation rules (§7) enforced
- Tests: token exchange (mocked token endpoint), refresh token rotation (new RT stored), proactive refresh, concurrent refresh lock, startup failure if exchange fails

### Phase 4 — Authorization Code + PKCE (1–2 weeks)

- `src/specmcp/runtime/oauth_handler.py` — `/auth/login`, `/auth/callback`, `/auth/status`, `/auth/session/<id>`
  - Security headers on all HTML responses (§4.5)
  - Nonce validation and consumption at `/auth/login`
  - OAuth error response handling at `/auth/callback`
  - `DELETE /auth/session/<id>` with loopback-only binding (§9.10)
- PKCE verifier generation (`secrets.token_bytes(32)` → base64url) and TTLCache storage
- State HMAC generation and verification per §9.6 (length-prefixed encoding)
- Scope validation after token exchange (§9.3)
- `OAuth2AuthorizationCodeScheme` implementing `AuthScheme`, with per-session refresh lock
- `AuthRequiredError` → dispatcher maps to LLM-facing nonce-URL message (§6)
- Config: `oauth2_authorization_code` type fully wired; all validation rules enforced
- Security tests: state mismatch rejected, expired state rejected, nonce single-use enforced, PKCE S256 verified, concurrent refresh lock, refresh token rotation, scope downgrade logged, `extra_params` reserved keys rejected, `http://` auth URLs rejected

### Phase 5 — Hardening (3–4 days)

- `specmcp init` generates correct OAuth config scaffolds (§11)
- Token store key rotation documentation and `specmcp token-store rotate` CLI command
- `token_endpoint_auth_method: basic | post` config option for non-compliant servers
- Management endpoint config (`bind: loopback | all`, `management_token_from`)
- Security review of the OAuth code path (required before v2 GA)
- Integration test: full Authorization Code flow end-to-end (test authorization server)

---

## 13. What This Document Defers

- **mTLS / custom signing schemes** — not planned for v2; `AuthScheme` protocol accommodates them as v3 additions
- **Token introspection** — validating tokens server-side before injecting; deferred until a customer need is identified
- **Multi-tenant token isolation** — ensuring session A cannot access session B's tokens; handled by the `session_id` key design (sessions are UUIDs, not user IDs) but not formally threat-modelled until v2 security review
- **Token revocation on session close** — calling upstream `/oauth/revoke` on disconnect; deferred to v2.1 based on user feedback
- **`token_endpoint_auth_method: post`** — body-form credential transmission for non-compliant servers; deferred to Phase 5

---

## Appendix: Open Questions

These must be resolved before Phase 4 coding begins:

1. **Which SQLite encryption library?** Options: `sqlcipher3` (full DB encryption, requires native build), `cryptography` (field-level AES-GCM, pure Python). Recommendation: `cryptography` field-level — no native dependency, easier to package in the `shiv` binary.

2. **`SERVER_SECRET` rotation.** If the operator rotates the HMAC signing key for the state parameter, in-flight OAuth flows (users mid-login) will fail. The UX impact is low (they just click the login link again), but it should be documented clearly in the operator runbook.

3. **Public vs. confidential OAuth clients.** Some providers (e.g. GitHub Apps) expect no `client_secret`. The `client_secret_from` field is optional in `oauth2_authorization_code`. Confirm that HTTP Basic Auth without a password (i.e. `client_id:` with empty secret) is handled correctly by the target providers, or fall back to body-form for public clients.

4. **Token store path default.** `~/.specmcp/tokens.db` is convenient for local dev but wrong for containerised deployments. Should the default be configurable at build time, or should the config always require an explicit path when using sqlite?

---

*This document was updated to Draft v0.2 after security review on 2026-05-23. All critical and high findings have been resolved in the design. Remaining medium and low findings are tracked as implementation requirements in Phases 4–5. This document should be re-reviewed after Phase 4 implementation before v2 GA.*
