# OAuth v2 Implementation Plan

**Status:** Ready for execution  
**Branch:** `feat/oauth`  
**Design doc:** `docs/oauth-design-v2.md` (Draft v0.2, security-reviewed)  
**Engineer note:** This plan is grounded in the actual v1 codebase as it exists today. Every file reference below names a real file; every change description reflects the actual current code.

---

## 1. Codebase Baseline

Before writing a line of OAuth code, every engineer on this feature must understand the current auth architecture. Here is what actually exists:

**`src/specmcp/auth/injector.py` — `AuthInjector`**  
The single auth object. Built once at startup from `Config`. Holds a `dict[str, ResolvedScheme]` (static credentials resolved from env) and a `dict[str, TokenCache]` (one per OAuth2 client-credentials scheme). The dispatcher calls `await injector.inject(op.auth, headers, params)` — it picks the first satisfiable auth group, applies credentials, and returns `(headers, params)` copies. There is no session parameter, no per-user token concept, and no `AuthScheme` protocol.

**`src/specmcp/auth/token_cache.py` — `TokenCache`**  
Per-scheme asyncio-locked token cache. Already handles the thundering-herd refresh problem for client credentials. The `CachedToken` holds `access_token: str` (plain str, not `SensitiveStr`) and `expires_at: float` (monotonic). The `invalidate()` method exists but is not yet called.

**`src/specmcp/runtime/dispatcher.py` — `dispatch()`**  
Step 7 calls `auth_injector.inject(op.auth, headers, params)`. No session awareness. Adding a `session` parameter is the minimal change needed to thread sessions through.

**`src/specmcp/cli/serve.py` — `_run_server` / `handle_call_tool`**  
Creates one `AuthInjector` for the server lifetime. `handle_call_tool(name, arguments)` has no session identity — every tool call in every session looks the same to the auth layer. This must change to support user-scoped tokens.

**`src/specmcp/config.py`**  
`SUPPORTED_CONFIG_VERSIONS = {"1"}`. `AuthSchemeConfig` union covers `apiKey`, `bearer`, `oauth2_client_credentials`. The `oauth2_authorization_code` type does not exist yet. `extra_params` has no reserved-field check.

**`src/specmcp/errors.py`**  
No `AuthRequiredError`. `TokenRefreshError` and `AuthConfigError` exist.

**HTTP transport:** Not implemented. `serve.py` returns an error if `--transport http` is passed. OAuth callbacks require HTTP endpoints — implementing HTTP transport is a hard prerequisite for Phase 4.

---

## 2. New Dependencies

Add these to `pyproject.toml` before Phase 1 coding begins. Pin lower bounds only.

```toml
dependencies = [
    # existing deps unchanged...
    "cachetools>=5.3.0",       # TTLCache for login nonces and PKCE verifiers
    "cryptography>=42.0.0",    # AES-256-GCM token encryption for SqliteTokenStore
    "starlette>=0.37.0",       # HTTP transport + OAuth callback endpoints
    "uvicorn>=0.29.0",         # ASGI server for HTTP transport
    "aiosqlite>=0.20.0",       # Async SQLite access for SqliteTokenStore
]
```

`cachetools` and `cryptography` are needed from Phase 1/3 respectively. `starlette` and `uvicorn` are needed for HTTP transport in Phase 4. All four should be added upfront so CI can validate the dependency graph on every commit.

---

## 3. Architecture Decision: Evolving `AuthInjector`, Not Replacing It

The design doc describes an `AuthScheme` protocol (with `apply()` and `handle_response()` per scheme). The current code uses `AuthInjector` with a flat `inject()` method. **Do not rewrite `AuthInjector` from scratch.** Instead, evolve it:

1. Add a `session: SessionContext | None` parameter to `inject()` — backward compatible because callers can pass `None`.
2. Add `handle_response(auth_requirements, request, response, session)` method alongside `inject()`.
3. Introduce an internal `_AuthSchemeHandler` protocol as a private detail inside `injector.py` — one handler class per auth type. This replaces the current `isinstance` dispatch chain and makes Phase 4 cleaner without changing the public API.

This approach avoids a big-bang refactor and keeps the v1 test suite green throughout.

---

## 4. Phase 1 — Session Plumbing (2–3 days)

Goal: introduce the session layer with zero behaviour change. No new auth types, no token store, no HTTP endpoints. After this phase the v1 test suite must be 100% green and the codebase is ready for Phase 2–4 work on the `feat/oauth` branch.

### 4.1 New file: `src/specmcp/runtime/session.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from specmcp.config import SensitiveStr

@dataclass
class SessionContext:
    session_id: str
    client_token: SensitiveStr | None = None
    metadata: dict = field(default_factory=dict)
```

Straightforward. No async, no I/O.

### 4.2 New file: `src/specmcp/auth/token_store.py`

Define the `TokenStore` ABC and `OAuthTokens` dataclass exactly as in the design doc §4.2. Implement `InMemoryTokenStore` using `dict[str, OAuthTokens]` with no locking (single async task per session is the invariant at this stage).

```python
@dataclass
class OAuthTokens:
    access_token: SensitiveStr
    refresh_token: SensitiveStr | None
    expires_at: float      # 0.0 = server did not supply expires_in
    token_type: str = "Bearer"
    scope: str = ""
```

**Important:** `access_token` is `SensitiveStr` here, unlike the existing `CachedToken.access_token` which is plain `str`. This is intentional — `OAuthTokens` may be persisted to disk (SQLite) while `CachedToken` is memory-only. Keep both types; they serve different purposes.

### 4.3 New file: `src/specmcp/auth/login_nonce.py`

`LoginNonceStore` backed by `cachetools.TTLCache(maxsize=10_000, ttl=300)`. Two async methods: `issue(session_id) -> str` and `consume(nonce) -> str | None`. Use a single `asyncio.Lock` around all TTLCache access.

### 4.4 Modify: `src/specmcp/errors.py`

Add two new error classes:

```python
class AuthRequiredError(AuthError):
    """No token for this session; user must authenticate via login URL."""
    code = "runtime.auth_required"

    def __init__(self, message: str, *, session_id: str | None, login_nonce: str | None, **kwargs):
        super().__init__(message, **kwargs)
        self.session_id = session_id
        self.login_nonce = login_nonce
```

Add `AuthRequiredError` to `MCP_ERROR_CONTENT`:
```python
AuthRequiredError: (
    "Authentication required. Please ask the user to visit: "
    "https://{host}/auth/login?nonce={exc.login_nonce}\n"
    "The link expires in 5 minutes. After logging in, retry your request."
),
```

Note: `{host}` will need to be injected at error-formatting time from server config. The template above is illustrative — the actual implementation should store the full pre-formatted URL on the exception object rather than relying on template interpolation.

### 4.5 Modify: `src/specmcp/runtime/dispatcher.py`

Add `session: SessionContext | None = None` to `dispatch()`. Pass it through to `auth_injector.inject()`. No other changes. The session is `None` for all calls until Phase 2 wires it up in `serve.py`.

```python
async def dispatch(
    *,
    tool: ToolDefinition,
    llm_args: dict[str, Any],
    http_client: HttpClient,
    auth_injector: AuthInjector,
    dispatch_config: DispatchConfig,
    operation_override: OperationOverride | None = None,
    request_id: str | None = None,
    session: SessionContext | None = None,     # NEW
) -> list[dict[str, Any]]:
```

### 4.6 Modify: `src/specmcp/auth/injector.py`

Add `session: SessionContext | None = None` to `inject()`. Pass it down to `_apply_group` and `_inject_scheme`. Current code does nothing with it — the `None` default means no behaviour change.

Also refactor the `isinstance` chain in `_inject_scheme` into an internal handler lookup — this sets up clean extension points for Phase 4:

```python
_HANDLERS: dict[type, _AuthSchemeHandler] = {
    ApiKeyAuthConfig: _ApiKeyHandler(),
    BearerAuthConfig: _BearerHandler(),
    OAuth2ClientCredentialsConfig: _ClientCredentialsHandler(),
}
```

### 4.7 Modify: `src/specmcp/cli/serve.py`

Add a session map: `_sessions: dict[str, SessionContext] = {}` at server scope.

The MCP SDK's `Server` provides a `server.get_client_id()` or similar mechanism to identify connections — **investigate the MCP SDK API before coding this**. If the SDK does not expose connection identity in `handle_call_tool`, use a `contextvars.ContextVar[str]` set when the session is first established.

For stdio transport (single-session), create one `SessionContext` at server start with a fixed UUID and reuse it for all calls. For HTTP transport (Phase 4), create one per connection.

### 4.8 Phase 1 tests

File: `tests/unit/runtime/test_session.py`
- `SessionContext` construction with and without `client_token`
- `SessionContext.metadata` defaults to empty dict

File: `tests/unit/auth/test_token_store.py`
- `InMemoryTokenStore`: get returns None before save; save then get round-trips; delete removes; all_sessions lists all
- `OAuthTokens` with `SensitiveStr` fields: `str()` returns `<redacted>`, `.reveal()` returns value

File: `tests/unit/auth/test_login_nonce.py`
- `issue()` returns a 43-char+ url-safe string
- `consume()` on a valid nonce returns session_id and removes nonce (single-use)
- `consume()` on the same nonce a second time returns None
- `consume()` on an unknown nonce returns None

File: `tests/unit/test_dispatcher.py` (extend existing)
- `dispatch()` with `session=None` behaves identically to before (no regression)

---

## 5. Phase 2 — Level 1: Client Token via Session Init (1–2 days)

Goal: MCP clients can pass a bearer token in `initialize.meta` and it is used for all tool calls in that session. This is the minimal feature for teams that manage their own tokens externally.

### 5.1 Investigate the MCP SDK's `initialize` hook

The `mcp.server.Server` class may expose an `on_initialize` callback or an `@server.initialize()` decorator. Read the MCP SDK source (`mcp>=1.0.0`) before coding. If no `initialize` hook exists, the approach is:

- Register a `@server.request_handler(InitializeRequest)` if that exists
- Or intercept the first message in the anyio task loop

This is the highest-risk unknown in Phase 2. Spike it on day 1 before writing production code.

### 5.2 Modify: `src/specmcp/cli/serve.py`

Once the hook is understood, read `params.meta.get("bearer_token")` from the initialize request. If present, create `SessionContext(session_id=..., client_token=SensitiveStr(value))`. Store in `_sessions`.

### 5.3 Modify: `src/specmcp/auth/injector.py`

In the `_BearerHandler` (or the existing `BearerAuthConfig` branch in `_inject_scheme`):

```python
# Client token from MCP session init takes priority over the env-var token
if session and session.client_token:
    headers["Authorization"] = f"Bearer {session.client_token.reveal()}"
    return
# Fall back to the static env-var token
assert resolved.credential is not None
headers["Authorization"] = f"Bearer {resolved.credential.reveal()}"
```

### 5.4 Phase 2 tests

File: `tests/unit/test_serve.py` (extend existing) or `tests/integration/test_session_init.py`
- Client passes `bearer_token` in `initialize.meta` → tool call gets `Authorization: Bearer <client-token>` header, not the env-var token
- Client passes no `meta` → env-var token is used as before (no regression)

---

## 6. Phase 3 — Client Credentials Hardening (3–4 days)

Goal: fix the H4 security finding (credentials in request body → HTTP Basic Auth), add `SqliteTokenStore` with encryption, and wire up full config version "2" validation.

### 6.1 Fix H4: Switch `_fetch_token` to HTTP Basic Auth

In `src/specmcp/auth/injector.py`, `_fetch_token()` currently builds a form dict containing `client_id` and `client_secret`. Change to:

```python
# BEFORE (sends credentials in body — non-compliant with RFC 6749 §2.3.1):
form = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
    **cfg.extra_params,
}

# AFTER (HTTP Basic Auth — RFC 6749 §2.3.1 recommended method):
form = {"grant_type": "client_credentials", **cfg.extra_params}
if cfg.scopes:
    form["scope"] = " ".join(cfg.scopes)

response = await client.post(
    cfg.token_url,
    auth=(client_id, client_secret),   # httpx handles Basic Auth encoding
    data=form,
    timeout=15.0,
)
```

**Update existing tests** in `tests/unit/auth/test_oauth2.py`:
- `test_fetch_token_sends_correct_form_fields` currently asserts `body["client_id"]` and `body["client_secret"]` in the POST body. Update to assert `Authorization: Basic ...` header instead.
- Add a test that `client_id` and `client_secret` are NOT in the request body.

### 6.2 Add `extra_params` reserved-field validation

In `src/specmcp/config.py`, add a `@field_validator` to `OAuth2ClientCredentialsConfig` and (later) `OAuth2AuthorizationCodeConfig`:

```python
_RESERVED_OAUTH_PARAMS = frozenset({
    "grant_type", "code", "code_verifier", "redirect_uri",
    "client_id", "client_secret", "response_type",
})

@field_validator("extra_params")
@classmethod
def check_extra_params(cls, v: dict[str, str]) -> dict[str, str]:
    reserved = _RESERVED_OAUTH_PARAMS & v.keys()
    if reserved:
        raise ValueError(
            f"extra_params must not contain reserved OAuth fields: {sorted(reserved)}"
        )
    return v
```

### 6.3 Add `https://` URL validation

In `src/specmcp/config.py`, add a validator to `OAuth2ClientCredentialsConfig.token_url` (and later `OAuth2AuthorizationCodeConfig`):

```python
@field_validator("token_url")
@classmethod
def check_token_url_scheme(cls, v: str) -> str:
    if not v.startswith("https://"):
        # Allow localhost for local dev/testing
        from urllib.parse import urlparse
        host = urlparse(v).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                f"token_url must use https:// in production. Got: {v!r}. "
                f"HTTP is only allowed for localhost."
            )
    return v
```

### 6.4 New file: `src/specmcp/auth/encryption.py`

AES-256-GCM helpers using `cryptography`:

```python
def derive_key(master_key: bytes, context: str) -> bytes:
    """Derive a 32-byte AES key from master_key using HKDF-SHA256."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context.encode())
    return hkdf.derive(master_key)

def encrypt(plaintext: str, key: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce + ciphertext + tag as bytes."""

def decrypt(ciphertext: bytes, key: bytes) -> str:
    """AES-256-GCM decrypt. Raises ValueError on authentication failure."""
```

Keep this file small and pure — no I/O, no config imports. Easy to unit-test in isolation.

### 6.5 New file: `src/specmcp/auth/token_store.py` — `SqliteTokenStore`

Add `SqliteTokenStore` to the existing file. Uses `aiosqlite` for async access. Schema:

```sql
CREATE TABLE IF NOT EXISTS oauth_tokens (
    session_id TEXT PRIMARY KEY,
    encrypted_blob BLOB NOT NULL,
    updated_at REAL NOT NULL
);
```

Each row stores `encrypted_blob = encrypt(json.dumps(tokens_dict), derived_key)`. The JSON dict contains all `OAuthTokens` fields; `SensitiveStr` fields are stored as plain strings inside the encrypted blob (the encryption provides confidentiality).

`SqliteTokenStore.__init__` takes `db_path: Path` and `encryption_key: bytes`. Derives a sub-key via `derive_key(encryption_key, "token_store_v1")` to allow future key versioning without changing the master key.

### 6.6 Config version "2"

In `src/specmcp/config.py`:

```python
SUPPORTED_CONFIG_VERSIONS = {"1", "2"}
```

Add `ManagementConfig` (from design doc §9.10):

```python
class ManagementConfig(BaseModel):
    bind: Literal["loopback", "all"] = "loopback"
    management_token_from: str | None = None
```

Add to `Config`:
```python
management: ManagementConfig = Field(default_factory=ManagementConfig)
```

Version "2" configs unlock `oauth2_authorization_code` (Phase 4). Version "1" configs continue to work as before.

### 6.7 Key rotation script: `scripts/token_store_rotate.py`

A standalone Python script (not a specmcp CLI command yet — that's Phase 5):

```
python scripts/token_store_rotate.py \
    --db ~/.specmcp/tokens.db \
    --old-key env(TOKEN_STORE_KEY) \
    --new-key env(TOKEN_STORE_KEY_NEW)
```

Reads all rows, decrypts with old key, re-encrypts with new key, writes back atomically via a transaction. Exits non-zero if any row fails.

### 6.8 Phase 3 tests

File: `tests/unit/auth/test_encryption.py`
- `encrypt` then `decrypt` round-trips
- `decrypt` raises `ValueError` on tampered ciphertext (authentication failure)
- Different contexts produce different derived keys

File: `tests/unit/auth/test_token_store.py` (extend)
- `SqliteTokenStore` CRUD: save, get, delete, all_sessions — using a `tmp_path` fixture
- Token values are not readable from the SQLite file without the key (assert raw sqlite3 row is opaque bytes)
- Wrong key raises `ValueError` on decrypt

File: `tests/unit/test_config.py` (extend)
- `extra_params` with reserved key raises `ConfigError` at load time
- `token_url: http://example.com` raises `ConfigError`
- `token_url: http://localhost:4444` is allowed
- Config version `"2"` loads successfully

File: `tests/unit/auth/test_oauth2.py` (update)
- `_fetch_token` uses HTTP Basic Auth, not body form (check `Authorization` header)
- `client_id` and `client_secret` absent from POST body

---

## 7. Phase 4 — Authorization Code + PKCE (1–2 weeks)

This is the most complex phase. Split it into four sub-milestones that can be reviewed independently.

### 7A: HTTP Transport (prerequisite for OAuth callback, 2 days)

OAuth callbacks require an HTTP endpoint. HTTP transport must be working before any callback code is written. This is a hard dependency.

**Modify `src/specmcp/cli/serve.py`:**

Add a Starlette ASGI app alongside the MCP server:

```python
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

async def _run_http_transport(server: Server, cfg: Config) -> None:
    host = cfg.transport.http.host
    port = cfg.transport.http.port

    # MCP SSE transport on /sse and /messages
    # OAuth routes mounted at /auth/*  (added in 7B)
    starlette_app = Starlette(routes=[
        Route("/sse", endpoint=mcp_sse_handler),
        Route("/messages", endpoint=mcp_message_handler, methods=["POST"]),
    ])

    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
```

Check the MCP SDK docs for the correct SSE transport integration. The MCP package includes `mcp.server.sse.SseServerTransport` — wire it up here.

**Tests:**
- HTTP transport binds to configured host/port
- `tools/list` over HTTP SSE returns the same tools as stdio
- `tools/call` over HTTP dispatches correctly (use `pytest-asyncio` with an in-process HTTP server)

### 7B: `OAuth2AuthorizationCodeConfig` in Config (1 day)

**Modify `src/specmcp/config.py`:**

```python
class OAuth2AuthorizationCodeConfig(BaseModel):
    type: Literal["oauth2_authorization_code"]
    authorization_url: str          # validated: https:// or localhost
    token_url: str                  # validated: https:// or localhost
    client_id_from: str
    client_secret_from: str | None = None   # optional: public clients
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str
    token_store: Literal["memory", "sqlite"] = "memory"
    token_store_path: str | None = None
    token_store_key_from: str | None = None    # required if token_store=sqlite
    extra_params: dict[str, str] = Field(default_factory=dict)
    
    # Validators: same https:// check as Phase 3, same extra_params check
    # Additional: if token_store=sqlite, token_store_key_from must be set
```

Update `AuthSchemeConfig` union:
```python
AuthSchemeConfig = (
    ApiKeyAuthConfig
    | BearerAuthConfig
    | OAuth2ClientCredentialsConfig
    | OAuth2AuthorizationCodeConfig
)
```

Update `parse_auth_schemes` in `Config.model_validator` to handle the new type. Reject `oauth2_authorization_code` if `config.version != "2"` with a clear error.

**Also add startup env var validation** in `Config` (or in `AuthInjector.build`):
- `SERVER_SECRET` must be set when any `oauth2_authorization_code` scheme exists
- `TOKEN_STORE_KEY` must be set when `token_store: sqlite` is used
- Both checks should happen at startup (not at first tool call)

**Tests:** All new config fields validate correctly; `oauth2_authorization_code` in a version "1" config raises `ConfigError`.

### 7C: OAuth HTTP Endpoints (3 days)

**New file: `src/specmcp/auth/pkce.py`**

```python
import base64, hashlib, os

def generate_verifier() -> str:
    """43-char base64url string, 256 bits of entropy (RFC 7636 §4.1)."""
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()

def generate_challenge(verifier: str) -> str:
    """S256 code challenge: base64url(SHA-256(verifier))."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
```

**New file: `src/specmcp/runtime/oauth_handler.py`**

A Starlette router with four routes (see design doc §4.5). Key implementation notes:

`GET /auth/login?nonce=<token>`:
1. `await nonce_store.consume(nonce)` → session_id, or return 400 if None
2. Generate `code_verifier` and `code_challenge` (PKCE)
3. Build `state = make_state(session_id, server_secret)` (see design §9.6)
4. Store `code_verifier` in `pkce_store[state]` (TTLCache, maxsize=10_000, ttl=600)
5. Build authorization URL with all required params; redirect

`GET /auth/callback?code=...&state=...`:
1. If `error` param present → log `oauth_callback_error`, render error page, return
2. `session_id = verify_state(state, server_secret)` or return 400
3. `code_verifier = pkce_store.pop(state)` or return 400 (replay protection)
4. POST to `token_url` with HTTP Basic Auth, `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier`
5. Validate returned scope vs `config.scopes` → log `scope_downgrade_detected` if narrower
6. Build `OAuthTokens`, `await token_store.save(session_id, tokens)`
7. Render success HTML page (minimal, includes security headers)

The `state` generation and verification functions go in `src/specmcp/auth/state.py`:

```python
import hmac, hashlib, struct, time

def make_state(session_id: str, secret: bytes) -> str:
    timestamp = int(time.time())
    msg = (
        struct.pack(">I", len(session_id))
        + session_id.encode()
        + struct.pack(">Q", timestamp)
    )
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{session_id}.{timestamp}.{sig}"

def verify_state(state: str, secret: bytes, ttl_seconds: int = 600) -> str | None:
    ...
```

**All HTML responses from `oauth_handler.py` must include:**
```python
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
```

### 7D: `OAuth2AuthorizationCodeScheme` and Dispatcher Integration (2 days)

**New file: `src/specmcp/auth/oauth2_authcode.py`**

Implements the `_AuthSchemeHandler` internal protocol for `oauth2_authorization_code`. Key methods:
- `apply(request, resolved, session)` — look up token from store; if missing/expired → try refresh; if still none → raise `AuthRequiredError`
- `handle_response(request, response, resolved, session)` — if 401, acquire session lock, call `_do_refresh`, return "retry" or "accept"
- `_do_refresh(session, tokens, resolved)` — exchange refresh token via HTTP Basic Auth; store new tokens (including new `refresh_token` from server); return True/False

The per-session refresh lock should live in a module-level `defaultdict(asyncio.Lock)` keyed by `session_id`.

**Modify `src/specmcp/auth/injector.py`:**

Add a `handle_response()` method:
```python
async def handle_response(
    self,
    auth_requirements: list[list[AuthRequirement]],
    request: httpx.Request,
    response: httpx.Response,
    session: SessionContext | None = None,
) -> Literal["accept", "retry"]:
```

**Modify `src/specmcp/runtime/dispatcher.py`:**

Update steps 7–9 to use `handle_response`:
```python
# Step 7: Send request
response = await http_client.request(...)

# Step 8: Handle 401 — try refresh
action = await auth_injector.handle_response(op.auth, request, response, session=session)
if action == "retry":
    # Re-inject (session now has refreshed token) and retry once
    req_headers, query_params = await auth_injector.inject(op.auth, ..., session=session)
    response = await http_client.request(...)

# Step 9: Format result
```

Also catch `AuthRequiredError` in the dispatcher and format the login-nonce message:
```python
except AuthRequiredError as exc:
    host = _get_server_host(dispatch_config)   # injected at serve time
    msg = (
        "Authentication required. Please ask the user to visit the following URL to log in:\n"
        f"https://{host}/auth/login?nonce={exc.login_nonce}\n\n"
        "The link expires in 5 minutes. After logging in, retry your request."
    )
    return [{"type": "text", "text": msg}]
```

The `host` needs to flow from `serve.py` into the dispatcher. Add `server_host: str | None = None` to `dispatch()` signature, defaulting to `None` (which produces a relative URL as a safe fallback).

### 7E: Phase 4 Tests

Security-focused tests are mandatory for this phase. Every finding from the design doc §9 must have a test:

File: `tests/unit/auth/test_pkce.py`
- `generate_verifier()` produces 43-char base64url string
- `generate_challenge(verifier)` matches the RFC 7636 test vector
- Uniqueness: two calls to `generate_verifier()` return different strings

File: `tests/unit/auth/test_state.py`
- `make_state` then `verify_state` round-trips
- `verify_state` returns None for expired state (mock `time.time`)
- `verify_state` returns None for tampered `session_id`
- `verify_state` returns None for tampered `sig`
- Ambiguous input test: `session_id="abc123"` + `timestamp="def"` cannot forge `session_id="abc"` + `timestamp="123def"` (length-prefix encoding proof)

File: `tests/unit/auth/test_oauth_handler.py`
- `/auth/login` with unknown nonce returns 400
- `/auth/login` with valid nonce redirects to authorization URL with correct PKCE params
- `/auth/login` consumes nonce (second visit with same nonce returns 400)
- `/auth/callback` with `?error=access_denied` returns error page, does not exchange code
- `/auth/callback` with valid code+state exchanges code, stores token
- `/auth/callback` with wrong `state` returns 400
- `/auth/callback` replayed state (second call with same state) returns 400
- All HTML responses include security headers (`Content-Security-Policy`, `X-Frame-Options`)

File: `tests/unit/auth/test_oauth2_authcode.py`
- `apply()` with valid token injects Bearer header
- `apply()` with expired token and refresh_token triggers `_do_refresh`, returns refreshed token
- `apply()` with no token raises `AuthRequiredError`
- `_do_refresh()` stores the NEW refresh_token from server response (not the old one)
- Concurrent refresh: two coroutines racing on expired token → only one exchange call (lock test)
- `handle_response()` on 401 calls `_do_refresh`, returns "retry"
- `handle_response()` on 200 returns "accept" without calling `_do_refresh`
- Scope downgrade: token endpoint returns narrower scope → `scope_downgrade_detected` event logged

File: `tests/unit/auth/test_login_nonce.py` (extend)
- Nonce store maxsize: filling beyond 10,000 evicts oldest entries

---

## 8. Phase 5 — Hardening (3–4 days)

### 8.1 `specmcp init` OAuth scaffolding

**Modify `src/specmcp/config.py` `Config.scaffold()`** and `src/specmcp/cli/init.py`:

When the upstream spec declares `oauth2` security schemes, generate the full `oauth2_authorization_code` config block with commented instructions and `.env.example` entries (see design doc §11).

### 8.2 `specmcp token-store rotate` CLI command

Promote `scripts/token_store_rotate.py` to a proper CLI command in `src/specmcp/cli/app.py`. Add tests.

### 8.3 `DELETE /auth/session/<id>` management endpoint

Implement with loopback-only binding (default) and optional management token check (if `bind: all`). Read `ManagementConfig` from `Config`.

### 8.4 `token_endpoint_auth_method` config option

For authorization servers that require body-form credentials:

```yaml
token_endpoint_auth_method: post   # basic (default) | post
```

### 8.5 Final security review gate

Before tagging v2.0.0-rc1, a second security review of the OAuth code paths is required (see design doc Phase 5). The review must cover: actual `_do_refresh` implementation, PKCE verifier generation, state HMAC, nonce store, token store encryption, and management endpoint access control.

### 8.6 Phase 5 tests

- `specmcp init` generates correct `oauth2_authorization_code` scaffold from a spec that declares `oauth2` security schemes
- `specmcp token-store rotate` migrates all rows (uses an in-memory SQLite fixture)
- `DELETE /auth/session/<id>` from loopback is accepted
- `DELETE /auth/session/<id>` from non-loopback without management token is rejected (when `bind: all`)

---

## 9. Cross-Cutting Concerns

### Structured logging

All new auth events use `structlog.get_logger()`. Required event names and fields:

| Event | Fields |
|---|---|
| `auth_required` | `session_id`, `scheme_name` |
| `auth_refresh_attempted` | `session_id`, `scheme_name` |
| `auth_refresh_failed` | `session_id`, `scheme_name` |
| `oauth_callback_error` | `error` (code only, not description) |
| `oauth_state_mismatch` | `session_id` |
| `login_nonce_invalid` | — (no session_id; nonce is invalid) |
| `scope_downgrade_detected` | `session_id`, `requested_scope`, `returned_scope` |
| `token_store_error` | `session_id`, `operation` |

Never log: `access_token`, `refresh_token`, `client_secret`, `code_verifier`, `error_description`.

### Keeping the v1 test suite green

Every commit on `feat/oauth` must pass the full test suite. Use this check:

```bash
uv run pytest tests/ -x --ignore=tests/integration
```

Integration tests (corpus, e2e) only run locally with `SPECMCP_RUN_CORPUS=1`.

### Branching and PR strategy

Each phase is one PR to `feat/oauth`. Phase 1–3 are safe to merge to `main` early (no user-visible changes in Phase 1, Level 1 tokens in Phase 2, CC hardening in Phase 3). Phase 4 must land as a single PR since it adds a new config type and live HTTP endpoints — merging it half-done would leave the server broken.

---

## 10. File Map

Summary of every file touched or created:

| File | Phase | Change type |
|---|---|---|
| `pyproject.toml` | Pre-work | Add 4 new deps |
| `src/specmcp/runtime/session.py` | 1 | New |
| `src/specmcp/auth/token_store.py` | 1, 3 | New (ABC + In-memory in P1; SQLite in P3) |
| `src/specmcp/auth/login_nonce.py` | 1 | New |
| `src/specmcp/errors.py` | 1 | Add `AuthRequiredError` |
| `src/specmcp/runtime/dispatcher.py` | 1, 4D | Add `session` param; add `AuthRequiredError` handling |
| `src/specmcp/auth/injector.py` | 1, 2, 3, 4D | Session param; Basic Auth; handler refactor; `handle_response` |
| `src/specmcp/cli/serve.py` | 2, 4A | Session map; HTTP transport |
| `src/specmcp/auth/encryption.py` | 3 | New |
| `src/specmcp/config.py` | 3, 4B | Version "2"; new config types; validators |
| `scripts/token_store_rotate.py` | 3 | New |
| `src/specmcp/auth/pkce.py` | 4C | New |
| `src/specmcp/auth/state.py` | 4C | New |
| `src/specmcp/runtime/oauth_handler.py` | 4C | New |
| `src/specmcp/auth/oauth2_authcode.py` | 4D | New |
| `src/specmcp/cli/init.py` | 5 | OAuth scaffold generation |
| `src/specmcp/cli/app.py` | 5 | `token-store rotate` command |

---

## 11. Risks and Mitigations

**Risk: MCP SDK `initialize` hook API is unclear.**  
This is the highest-risk unknown. Spike Phase 2 on day 1. If no clean hook exists, consider a thin wrapper around `Server` that intercepts the initialize message before passing it to the SDK. Do not block Phase 1 on this — Phase 1 has no dependency on the initialize hook.

**Risk: HTTP transport + Starlette integration with the MCP SDK.**  
The MCP SDK's `SseServerTransport` may have opinionated assumptions about how it's mounted. Test the wiring with a minimal `tools/list` call over HTTP SSE before building OAuth routes on top of it. If the SDK's SSE transport is difficult to embed in Starlette, evaluate `mcp.server.fastapi` (if it exists) as an alternative.

**Risk: Phase 4 is large.**  
If Phase 4 runs long, split it: merge 7A (HTTP transport) and 7B (config types) to `feat/oauth` early as they are independently testable. Keep 7C–7D together — the callback handler and the dispatcher integration are tightly coupled.

**Risk: SQLite locking under concurrent sessions.**  
`aiosqlite` uses a single write lock per connection. For specmcp's expected workload (token refreshes are infrequent — once per access token lifetime), this is unlikely to be a bottleneck. If it is, the fix is a write queue (one writer, many readers via `aiosqlite`'s WAL mode). Profile before optimising.

---

*This plan should be reviewed by the engineer(s) implementing it before Phase 1 begins. Phases 1–3 can be executed by one engineer sequentially. Phase 4 benefits from two engineers in parallel (7A/7B on one track, 7C/7D review prep on the other).*
