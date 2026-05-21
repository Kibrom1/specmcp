# specmcp v1.1 — Feature Gap Implementation Plan

**Author:** Staff Engineer review  
**Status:** Proposed  
**Scope:** Three feature gaps identified post v1.0: OAuth 2.0 (client_credentials), SSE streaming, and `--watch` mode  
**Target version:** v1.1.0

---

## Executive summary

All three gaps are additive — they extend existing abstractions without requiring a redesign.
The work is sequenced so each gap can ship independently and each one reduces risk for the
next. `--watch` is the lowest-risk and highest-developer-experience return; OAuth 2.0 unlocks
the largest surface area of real-world APIs; SSE streaming is narrow in scope but
architecturally the deepest change.

**Recommended ship order:** `--watch` → OAuth 2.0 → SSE streaming

---

## Codebase snapshot (v1.0 state)

Before planning changes, the key invariants that must be preserved:

| Invariant | Where enforced |
|---|---|
| `SensitiveStr.reveal()` is the only credential escape point | `auth/injector.py:166` |
| `ArgumentMap` is the single source of truth for HTTP construction | `runtime/dispatcher.py` |
| The pipeline (Load→Normalize→Simplify→Expose) is a pure function | `cli/serve.py:95–116` |
| `AuthInjector` is built once and held immutably | `cli/serve.py:124` |
| `HttpClient` uses `trust_env=False` | `runtime/http_client.py:83` |
| `inject()` is synchronous | `auth/injector.py:92` |
| `handle_call_tool` returns `list[TextContent]` | `cli/serve.py:186` |

The last two are the constraints that drive the most architectural decisions below.

---

## Gap 1: `--watch` mode

### Problem statement

During development a user edits their spec or config and has to `Ctrl-C` + restart the
server. Every restart drops the stdio connection and requires the MCP client to
reconnect. `--watch` should reload the ToolRegistry in place without dropping the
connection.

### Current architecture

```python
# serve.py — pipeline runs once at startup, captured in closures
registry = ToolRegistry.build(simplified_ops, config=cfg)

@server.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    # `registry` is a captured, immutable variable
    return [mcp_types.Tool(...) for tool in registry.tools]
```

The problem is `registry` is a plain captured variable. Swapping it requires the closures
to read through an indirection.

### Proposed design

**New module: `specmcp/runtime/registry_ref.py`**

```python
import asyncio
from dataclasses import dataclass, field
from specmcp.core.expose import ToolRegistry

@dataclass
class RegistryRef:
    """Mutable reference to the current ToolRegistry, safe for asyncio use.

    Reads are lock-free: asyncio's single-threaded event loop means that
    `self._registry = new_registry` is an atomic pointer swap — no coroutine
    can observe a half-written value. The lock is held only during swap() so
    that a slow reload does not block concurrent tools/list or tools/call calls.
    """
    _registry: ToolRegistry
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get(self) -> ToolRegistry:
        """Return the current registry. Lock-free; safe to call from any coroutine."""
        return self._registry

    async def swap(self, new_registry: ToolRegistry) -> None:
        """Atomically replace the registry. Blocks until any in-progress swap completes."""
        async with self._lock:
            self._registry = new_registry
```

**`_run_pipeline()` helper (new private function in `serve.py`)**

The watcher needs to re-run the full pipeline. Extract it from `serve_cmd` so both the
startup path and the reload path share the same code:

```python
def _run_pipeline(
    spec_source: str,
    cfg: Config | None,
) -> ToolRegistry:
    """Load → Normalize → Simplify → Expose. Returns a fresh ToolRegistry.

    Raises SpecmcpError on any pipeline failure (caller should catch and keep
    the existing registry rather than crashing the server).
    """
    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    _, resolved = load_spec(spec_source)
    ops = normalize(
        resolved,
        base_url_override=cfg.server.base_url_override if cfg else None,
        include_deprecated=cfg.server.include_deprecated if cfg else True,
        include_tags=cfg.server.include_tags if cfg else None,
        exclude_tags=cfg.server.exclude_tags if cfg else None,
        include_operations=cfg.server.include_operations if cfg else None,
        exclude_operations=cfg.server.exclude_operations if cfg else None,
    )
    simplify_cfg = cfg.simplify if cfg else SimplifyConfig()
    simplified = simplify(ops, config=simplify_cfg)
    return ToolRegistry.build(simplified, config=cfg)
```

**Changes to `serve.py`** — closures read through `RegistryRef`

```python
# Before: closures capture `registry` directly
# After: closures read through RegistryRef.get() (lock-free)

registry_ref = RegistryRef(registry)

@server.list_tools()
async def handle_list_tools():
    reg = registry_ref.get()
    return [mcp_types.Tool(...) for tool in reg.tools]

@server.call_tool()
async def handle_call_tool(name, arguments):
    reg = registry_ref.get()
    tool = reg.lookup(name)
    ...
```

**File watcher and `anyio` task group structure**

Add `watchfiles>=0.21` to dependencies. It is async-native (no threading), actively
maintained, and used by FastAPI/uvicorn for the same purpose.

```python
from watchfiles import awatch

async def _watch_and_reload(
    spec_source: str,
    config_path: Path | None,
    registry_ref: RegistryRef,
    cfg: Config | None,
) -> None:
    paths_to_watch: set[str] = set()
    if Path(spec_source).exists():  # local file only; URLs cannot be watched
        paths_to_watch.add(spec_source)
    if config_path and config_path.exists():
        paths_to_watch.add(str(config_path))

    if not paths_to_watch:
        return  # watch mode silently no-ops for URL-sourced specs

    async for changes in awatch(*paths_to_watch):
        typer.echo(f"[watch] change detected: {changes}. Reloading...", err=True)
        try:
            new_registry = _run_pipeline(spec_source, cfg)
            await registry_ref.swap(new_registry)
            typer.echo(f"[watch] reloaded {len(new_registry.tools)} tools.", err=True)
        except SpecmcpError as exc:
            # Keep the previous registry live — the server stays up.
            typer.echo(f"[watch] reload failed: {exc}. Keeping previous registry.", err=True)
```

The `_run_server()` function must be restructured to launch the watcher as a sibling
task inside `anyio.create_task_group()`. The complete async structure becomes:

```python
async def _run_server(registry, auth_injector, dispatch_cfg, cfg, transport, watch, config_path, spec_source):
    from specmcp.runtime.http_client import HttpClient
    from specmcp.runtime.registry_ref import RegistryRef

    registry_ref = RegistryRef(registry)

    async with HttpClient(dispatch_cfg) as http_client:
        # ... register handlers (list_tools, call_tool) using registry_ref.get() ...

        async with anyio.create_task_group() as tg:
            # Task 1: MCP transport (stdio)
            if transport == "stdio":
                tg.start_soon(_run_stdio, server)
            # Task 2: file watcher (only if --watch and spec is a local file)
            if watch:
                tg.start_soon(
                    _watch_and_reload, spec_source, config_path, registry_ref, cfg
                )
```

**Known limitation: `AuthInjector` is not rebuilt on reload**

When `--watch` detects a config change, only the `ToolRegistry` is replaced. The
`AuthInjector` — which holds resolved credentials — is **not** rebuilt. This means:

- Changes to operation filters, tags, descriptions → picked up immediately ✅
- Changes to `auth:` section of `mcp.config.yaml` → **require a server restart** ❌

Document this prominently in the `--watch` help text and in the user docs. A full
`AuthInjector` rebuild on watch events is deferred to v1.2 (it requires catching and
surfacing credential resolution errors in the watcher loop, and rebuilding the
`TokenCache` state for OAuth schemes).

### CLI change

```python
@app.command("serve")
def serve_cmd(
    ...
    watch: bool = typer.Option(False, "--watch", "-w", help="Reload on spec/config change."),
):
```

### Files touched

| File | Change |
|---|---|
| `specmcp/runtime/registry_ref.py` | **New** — `RegistryRef` dataclass |
| `specmcp/cli/serve.py` | closures read through `RegistryRef`; `--watch` flag; watcher task |
| `pyproject.toml` | add `watchfiles>=0.21` to dependencies |
| `specmcp/config.py` | no changes |
| `specmcp/auth/injector.py` | no changes |

### Risk assessment

**Low.** The pipeline is already a pure function. `RegistryRef` is a trivial indirection.
The lock prevents a race between a reload and a concurrent `tools/call`. The only edge
case is a reload that takes longer than the LLM's tool call timeout — mitigated by
keeping the old registry live until swap completes.

URL-sourced specs (e.g. `--spec https://...`) silently skip the watcher; document this.

### Test strategy

- Unit: `RegistryRef` swap is atomic under simulated concurrent reads
- Integration: write a temp spec file, start watch mode, overwrite the file, assert
  `tools/list` returns updated tools without reconnecting the client
- Negative: corrupt the spec mid-write, assert the old registry is still served

### Effort estimate

**1.5–2 days** including tests and documentation.

---

## Gap 2: OAuth 2.0 (client_credentials flow)

### Problem statement

`BearerAuthConfig` accepts a static token from an env var. Modern APIs (Stripe, Google,
Azure, Salesforce) require OAuth 2.0 tokens that expire, typically using the
`client_credentials` flow for server-to-server auth. Without this, specmcp cannot be
used with the majority of enterprise APIs.

### Scope boundary

This plan covers **client_credentials only**. `authorization_code` (interactive user
login) is explicitly out of scope for v1.1 — it requires a redirect URI and a browser
flow that has no clean analog in a stdio MCP server.

### Current architecture gap

```python
# config.py — line 113
AuthSchemeConfig = ApiKeyAuthConfig | BearerAuthConfig

# injector.py — inject() is synchronous
def inject(self, auth_requirements, *, headers, params) -> tuple[...]:
    ...
    value = resolved.credential.reveal()  # static value, resolved once at startup
```

OAuth tokens must be fetched at runtime and refreshed before expiry. This requires:
1. `inject()` to become `async` (token fetch is a network call)
2. A mutable token cache (breaking the `ResolvedScheme` frozen constraint)
3. A new config model and new error types

### Proposed design

#### New config model

```python
# config.py — new addition
class OAuth2ClientCredentialsConfig(BaseModel):
    type: Literal["oauth2_client_credentials"]
    token_url: str                    # https://auth.example.com/token
    client_id_from: str               # env(MY_CLIENT_ID)
    client_secret_from: str           # env(MY_CLIENT_SECRET)
    scopes: list[str] = Field(default_factory=list)
    # Optional: audiences, extra form fields for non-standard token endpoints
    extra_params: dict[str, str] = Field(default_factory=dict)

AuthSchemeConfig = ApiKeyAuthConfig | BearerAuthConfig | OAuth2ClientCredentialsConfig
```

`client_id` and `client_secret` are resolved via the same `env(VAR)` DSL as other
credentials, so the existing `_resolve_value_from()` helper applies unchanged.

#### Token cache

`ResolvedScheme` is frozen and holds a `SensitiveStr` credential — a static value.
OAuth needs a mutable cache. Two options were considered:

**Option A:** Make `ResolvedScheme` a regular (non-frozen) dataclass and add an optional
`token_cache: TokenCache | None` field.

**Option B:** Keep `ResolvedScheme` frozen and put the mutable cache inside `AuthInjector`
keyed by scheme name.

**Decision: Option B.** It preserves the frozen invariant on `ResolvedScheme` (which
is important for correctness — it is a resolved credential, not a session object) and
keeps the mutation in one place.

```python
# New module: specmcp/auth/token_cache.py

import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class CachedToken:
    access_token: str
    expires_at: float  # Unix timestamp

    def is_expired(self, buffer_seconds: float = 60.0) -> bool:
        return time.monotonic() >= self.expires_at - buffer_seconds


@dataclass
class TokenCache:
    """Per-scheme OAuth token cache with async refresh lock.
    
    The lock prevents thundering-herd refreshes when multiple concurrent
    tool calls race on an expired token.
    """
    _token: CachedToken | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_or_refresh(
        self,
        refresh_fn,  # async callable () -> CachedToken
    ) -> str:
        async with self._lock:
            if self._token is None or self._token.is_expired():
                self._token = await refresh_fn()
            return self._token.access_token
```

#### `inject()` becomes `async`

```python
# auth/injector.py
async def inject(
    self,
    auth_requirements: list[list[AuthRequirement]],
    *,
    headers: dict[str, str],
    params: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    ...
    for req in group:
        resolved = self._schemes[req.scheme_name]
        await self._inject_scheme(resolved, out_headers, out_params)

async def _inject_scheme(self, resolved, headers, params) -> None:
    cfg = resolved.config
    if isinstance(cfg, OAuth2ClientCredentialsConfig):
        cache = self._token_caches[resolved.scheme_name]
        token = await cache.get_or_refresh(
            lambda: self._fetch_token(cfg, resolved)
        )
        headers["Authorization"] = f"Bearer {token}"
    elif isinstance(cfg, BearerAuthConfig):
        headers["Authorization"] = f"Bearer {resolved.credential.reveal()}"
    elif isinstance(cfg, ApiKeyAuthConfig):
        ...
```

#### `ResolvedScheme` model changes

`ResolvedScheme` (defined in `auth/injector.py`) needs two new optional fields to carry
the OAuth credentials through to `_fetch_token()`. These are populated in `build()` and
are `None` for non-OAuth schemes:

```python
@dataclass(frozen=True)
class ResolvedScheme:
    scheme_name: str
    config: AuthSchemeConfig
    credential: SensitiveStr | None = None        # existing — used by apiKey/bearer
    oauth_client_id: SensitiveStr | None = None   # NEW — OAuth only
    oauth_client_secret: SensitiveStr | None = None  # NEW — OAuth only
```

#### `AuthInjector.build()` changes

`build()` must initialise the `_token_caches` dict and resolve OAuth credentials:

```python
@classmethod
def build(cls, cfg: Config | None) -> "AuthInjector":
    schemes: dict[str, ResolvedScheme] = {}
    token_caches: dict[str, TokenCache] = {}  # NEW

    for scheme_name, scheme_cfg in (cfg.auth if cfg else {}).items():
        if isinstance(scheme_cfg, OAuth2ClientCredentialsConfig):
            client_id = SensitiveStr(_resolve_value_from(scheme_cfg.client_id_from))
            client_secret = SensitiveStr(_resolve_value_from(scheme_cfg.client_secret_from))
            schemes[scheme_name] = ResolvedScheme(
                scheme_name=scheme_name,
                config=scheme_cfg,
                oauth_client_id=client_id,
                oauth_client_secret=client_secret,
            )
            token_caches[scheme_name] = TokenCache()  # empty cache, populated on first use
        elif isinstance(scheme_cfg, BearerAuthConfig):
            credential = SensitiveStr(_resolve_value_from(scheme_cfg.token_from))
            schemes[scheme_name] = ResolvedScheme(
                scheme_name=scheme_name, config=scheme_cfg, credential=credential
            )
        elif isinstance(scheme_cfg, ApiKeyAuthConfig):
            # ... existing logic ...

    return cls(_schemes=schemes, _token_caches=token_caches)
```

#### `_fetch_token()` — network call

```python
async def _fetch_token(
    self,
    cfg: OAuth2ClientCredentialsConfig,
    resolved: ResolvedScheme,
) -> CachedToken:
    """Exchange client credentials for an access token.

    Opens a dedicated httpx.AsyncClient rather than reusing the main HttpClient.
    This is intentional: the auth layer must not be coupled to the runtime layer,
    and token fetches are infrequent (one per expiry window, typically 1 hour).
    trust_env=False matches the main HttpClient policy.
    """
    import time

    assert resolved.oauth_client_id is not None
    assert resolved.oauth_client_secret is not None

    client_id = resolved.oauth_client_id.reveal()
    client_secret = resolved.oauth_client_secret.reveal()

    form: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        **cfg.extra_params,
    }
    if cfg.scopes:
        form["scope"] = " ".join(cfg.scopes)

    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.post(cfg.token_url, data=form, timeout=15.0)
        except httpx.RequestError as exc:
            raise TokenRefreshError(
                f"Failed to reach OAuth token endpoint {cfg.token_url}: {exc}"
                # Note: cfg.token_url is not sensitive; client_id/secret are never included
            ) from exc

    if r.status_code != 200:
        raise TokenRefreshError(
            f"OAuth token endpoint returned HTTP {r.status_code}",
            status_code=r.status_code,
        )

    body = r.json()
    if "access_token" not in body:
        raise TokenRefreshError("OAuth token response missing 'access_token'")

    expires_in = float(body.get("expires_in", 3600))
    return CachedToken(
        access_token=body["access_token"],   # never logged — only used in Authorization header
        expires_at=time.monotonic() + expires_in,
    )
```

**Security note:** `access_token` is a plain string in `CachedToken`, not a `SensitiveStr`.
It is never stored in `ResolvedScheme` (a frozen, inspectable object), only in `CachedToken`
which lives inside `TokenCache` inside `AuthInjector`. It must not appear in any error
message or log line — review all `TokenRefreshError` raise sites before shipping.
Wrapping it in `SensitiveStr` is deferred to v1.2 (tracked in the security table below).

#### Cascade of `async` propagation

Making `inject()` async cascades into the dispatcher:

```python
# runtime/dispatcher.py — already async, no signature change needed
req_headers, query_params = await auth_injector.inject(...)  # add await
```

The dispatcher is already `async def dispatch(...)`, so this is a one-word change.

#### New error types

```python
# errors.py
class TokenRefreshError(AuthError):
    """OAuth token endpoint request failed or returned an unexpected response."""
    code = "runtime.token_refresh_error"

    def __init__(self, message, *, status_code: int | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.status_code = status_code
        if status_code:
            self.context["status_code"] = status_code
```

#### `mcp.config.yaml` example for OAuth

```yaml
auth:
  stripeOAuth:
    type: oauth2_client_credentials
    token_url: https://api.stripe.com/v1/oauth/token
    client_id_from: env(STRIPE_CLIENT_ID)
    client_secret_from: env(STRIPE_CLIENT_SECRET)
    scopes:
      - read_write
```

### Files touched

| File | Change |
|---|---|
| `specmcp/config.py` | Add `OAuth2ClientCredentialsConfig`; update `AuthSchemeConfig` union; update `Config.scaffold()` |
| `specmcp/auth/token_cache.py` | **New** — `CachedToken`, `TokenCache` |
| `specmcp/auth/injector.py` | `ResolvedScheme` gets `oauth_client_id` + `oauth_client_secret` fields; `inject()` → `async`; `_inject_scheme()` → `async`; add `_fetch_token()`; `build()` initialises `_token_caches` dict |
| `specmcp/runtime/dispatcher.py` | `await auth_injector.inject(...)` (add `await`) |
| `specmcp/errors.py` | Add `TokenRefreshError(AuthError)` |
| `pyproject.toml` | No new deps — `httpx` is already a dependency |
| `tests/unit/auth/test_token_cache.py` | **New** — unit tests for cache, expiry, lock behaviour |
| `tests/unit/auth/test_oauth2.py` | **New** — `_fetch_token` with mocked httpx responses |
| `tests/integration/test_serve_e2e.py` | Add OAuth scenario with respx-mocked token endpoint |

### Risk assessment

**Medium.** The `inject()` → `async` change is the highest-risk item because it touches
the hot path of every tool call. However, because `dispatch()` is already async the
caller-side change is a single `await`. The token cache lock prevents thundering-herd
refreshes. The most likely production failure mode is an OAuth endpoint that returns a
non-standard `expires_in` — the 60-second expiry buffer in `is_expired()` mitigates this.

**Do not** add token persistence to disk in v1.1. In-memory cache is sufficient and avoids
a class of security issues around file permissions.

### Test strategy

- Unit: `TokenCache.get_or_refresh()` — first call fetches; subsequent calls hit cache;
  expired token triggers refresh; concurrent callers don't double-refresh (use asyncio
  tasks to simulate concurrency)
- Unit: `_fetch_token()` with `respx`-mocked token endpoint — 200 success, 400 error,
  malformed body (missing `access_token`), network error
- Integration: full E2E test where `tools/call` hits a respx-mocked token endpoint before
  forwarding to the upstream API; assert token appears in `Authorization: Bearer` header

### Effort estimate

**3–4 days** including tests. The async propagation through the injector is mechanical
but needs careful review to ensure no sync/async mismatch is introduced.

---

## Gap 3: SSE streaming responses

### Problem statement

Some APIs (OpenAI chat completions, Anthropic streaming, GitHub Copilot) return
`text/event-stream` responses that stream tokens or events incrementally. The current
`HttpClient` buffers the full body before returning, which blocks indefinitely on a
never-ending stream and will trip `response_size_limit_bytes` on large ones.

### Scope boundary

v1.1 targets **response-only SSE** (server-push). Request streaming (uploading a body
as a stream) is not in scope. WebSockets are not in scope.

### Current architecture gap

Three layers need to change:

1. **`HttpClient`** — `_send_once()` calls `client.request()` which buffers the full body.
   A streaming path must use `client.stream()` and yield parsed SSE events.

2. **`Dispatcher`** — currently calls `http_client.request()` and gets back `HttpResponse`.
   A streaming call would need a different return type.

3. **`serve.py`** — `handle_call_tool` returns `list[TextContent]`.
   MCP streaming requires the handler to become an async generator.

### MCP SDK streaming support

The current `mcp.server.Server` `@call_tool()` handler signature returns
`list[TextContent]`. The MCP spec (as of MCP 1.x) allows progressive streaming via
multiple content blocks, but the Python SDK's `Server` class in v1.x does not expose
an async generator handler natively.

**Architectural decision:** rather than wait for the SDK to support generator handlers,
**buffer SSE events into a single text block per response**. The LLM sees the full
streamed output as one content block, delivered after the stream closes. This is the
correct v1.1 approach:

- It requires no MCP SDK changes
- It works correctly for the LLM use case (the model reads the complete response)
- It does not solve real-time streaming to the *LLM* (that requires MCP SDK support for
  generator handlers, which is a future concern)
- It does solve the **current blocker**: a `text/event-stream` response causing an
  indefinite hang or size limit error

When the MCP SDK adds generator support, the buffering shim can be replaced with a
true streaming handler with no changes to `HttpClient` or `Dispatcher`.

### Proposed design

#### Detection

The `Operation` model already has `responses: list[Response]`, and `ResponseVariant`
has `content_type: str`. The Normalize stage records the declared response content
types from the spec. At dispatch time, the dispatcher checks the declared response type:

```python
def _operation_may_stream(op: Operation) -> bool:
    """Return True if any declared 2xx response is text/event-stream."""
    for resp in op.responses:
        if resp.status_code.startswith("2"):
            for v in resp.variants:
                if "event-stream" in v.content_type:
                    return True
    return False
```

Note: the actual response content type from the upstream server is the ground truth at
runtime. The declared check is an optimisation to skip the streaming path for non-SSE
operations entirely.

#### `HttpClient` — new streaming method

```python
# runtime/http_client.py

async def stream_request(
    self,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, str],
    json_body: Any | None,
    timeout_seconds: float,
    streaming_max_bytes: int,
    request_id: str,
) -> tuple[str, bool]:
    """Stream an SSE response and return (concatenated_event_data, truncated).

    Parses `data:` lines per the SSE spec (WHATWG). The `[DONE]` sentinel
    (OpenAI-style) is recognised as a stream terminator; other APIs close
    the connection instead, which is handled naturally by aiter_lines().

    Raises:
        UpstreamClientError: on 4xx or 5xx responses.
        TransientError: on network failures or timeout.
    """
    assert self._client is not None
    full_text_parts: list[str] = []
    total_bytes = 0
    truncated = False

    try:
        async with self._client.stream(
            method, url, headers=headers, params=params,
            json=json_body, timeout=timeout_seconds
        ) as response:
            # Handle error status codes before reading the body
            if 400 <= response.status_code < 500:
                body = await response.aread()
                raise UpstreamClientError(
                    f"Upstream returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    body=body.decode("utf-8", errors="replace"),
                    request_id=request_id,
                )
            if response.status_code >= 500:
                body = await response.aread()
                raise UpstreamServerError(
                    f"Upstream returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    body=body.decode("utf-8", errors="replace"),
                    request_id=request_id,
                )

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        # OpenAI-style terminator; other APIs close the
                        # connection instead, which ends aiter_lines() naturally.
                        break
                    total_bytes += len(data.encode("utf-8"))
                    if total_bytes > streaming_max_bytes:
                        truncated = True
                        break
                    full_text_parts.append(data)

    except httpx.TimeoutException as exc:
        raise TransientError(f"SSE stream timed out: {url}") from exc
    except httpx.RequestError as exc:
        raise TransientError(f"SSE stream error from {url}: {exc}") from exc

    return "\n".join(full_text_parts), truncated
```

#### `Dispatcher` — streaming branch

The spec-declared content type (`_operation_may_stream`) is used as the fast path to
skip stream setup for the common case. At runtime, the actual response `Content-Type`
is the ground truth: if the upstream returns `application/json` despite declaring
`text/event-stream` (e.g. on error or because the stream was conditional), the
dispatcher falls back to normal buffering. This prevents the SSE parser from returning
an empty string on a valid JSON response.

```python
# runtime/dispatcher.py — inside dispatch()

# 9. HTTP call
if dispatch_config.enable_streaming and _operation_may_stream(op):
    streaming_timeout = timeout * dispatch_config.streaming_timeout_multiplier
    raw_text, truncated = await http_client.stream_request(
        method=op.method, url=url, headers=req_headers,
        params=query_params, json_body=json_body,
        timeout_seconds=streaming_timeout,
        streaming_max_bytes=dispatch_config.streaming_max_bytes,
        request_id=request_id,
    )
    text = raw_text
    if truncated:
        text += "\n\n[Response truncated]"
    return [{"type": "text", "text": text}]
else:
    response = await http_client.request(
        method=op.method, url=url, headers=req_headers,
        params=query_params, json_body=json_body, form_body=form_body,
        timeout_seconds=timeout, retry_config=retry,
        request_id=request_id,
    )
    return _format_result(response)
```

**Runtime content-type fallback** — `stream_request()` should check the response
`Content-Type` header before entering the SSE parse loop. If the upstream returns a
non-SSE content type, fall back to buffering the full body and returning it as text:

```python
# Inside stream_request(), after status code checks, before aiter_lines():
actual_ct = response.headers.get("content-type", "")
if "event-stream" not in actual_ct:
    # Upstream declared SSE but responded with a different content type.
    # Buffer and return as plain text to avoid returning an empty string.
    body = await response.aread()
    return body.decode("utf-8", errors="replace"), False
```

#### `DispatchConfig` — new fields

```python
class DispatchConfig(BaseModel):
    ...
    # Multiplier applied to the resolved timeout for SSE streaming operations.
    # A 30s default timeout becomes 150s for streaming. Per-operation
    # timeout_seconds overrides are also multiplied; document this clearly.
    streaming_timeout_multiplier: float = Field(default=5.0, ge=1.0)
    # Maximum bytes to buffer from an SSE stream before truncating.
    # Prevents OOM on indefinite or misbehaving streams.
    streaming_max_bytes: int = Field(default=4 * 1024 * 1024)  # 4 MiB
    # Set to False to disable streaming and always buffer (useful for debugging).
    enable_streaming: bool = True
```

### Files touched

| File | Change |
|---|---|
| `specmcp/runtime/http_client.py` | Add `stream_request()` method |
| `specmcp/runtime/dispatcher.py` | Add `_operation_may_stream()`; streaming branch in `dispatch()` |
| `specmcp/config.py` | Add `streaming_timeout_multiplier`, `enable_streaming` to `DispatchConfig` |
| `tests/unit/runtime/test_streaming.py` | **New** — SSE parsing, `[DONE]` handling, network errors |
| `tests/integration/test_serve_e2e.py` | Add streaming scenario with `respx` SSE mock |

### Risk assessment

**Medium-high.** The main risks are:

1. **Timeout sizing** — SSE streams can run for minutes. The 5x multiplier is a heuristic.
   Per-operation `timeout_seconds` in `OperationOverride` already exists and should be
   the primary control; document this clearly.

2. **Non-standard SSE** — The parser above handles `data:` lines and `[DONE]`. Some APIs
   use `event:` fields, JSON-wrapped data, or multi-line events. The v1.1 parser handles
   the common case (OpenAI-style). Add a `SimplifyWarning.kind="sse_non_standard"` for
   operations where the spec declares SSE but the parser may not handle the full format.

3. **Memory** — Buffering a long stream defeats the purpose of streaming from a memory
   perspective. Add a `streaming_max_bytes` cap (default 4 MiB) that truncates and
   appends `[Response truncated]` to prevent OOM on indefinite streams.

### Test strategy

- Unit: `stream_request()` with `respx` SSE mock — multi-event stream, `[DONE]` stops
  iteration, 4xx mid-stream, network timeout
- Unit: `_operation_may_stream()` — positive (event-stream declared), negative (json only)
- Integration: full E2E with streaming petstore extension

### Effort estimate

**3–4 days** including tests and the `streaming_max_bytes` guard.

---

## Sequencing and milestones

```
Week 1
  M1.1 — --watch mode (2 days)
    - registry_ref.py
    - serve.py watcher task
    - watchfiles dep
    - Tests

Week 2–3
  M1.2 — OAuth 2.0 client_credentials (4 days)
    - OAuth2ClientCredentialsConfig
    - token_cache.py
    - injector.py async migration
    - dispatcher.py await change
    - TokenRefreshError
    - Tests

Week 4
  M1.3 — SSE streaming (4 days)
    - http_client.py stream_request
    - dispatcher.py streaming branch
    - DispatchConfig additions
    - Tests
```

Each milestone is independently releasable. M1.1 has no dependencies. M1.2 depends
on no prior milestone but must be complete before M1.3 (the streaming timeout config
builds on the DispatchConfig changes from M1.2).

---

## Cross-cutting concerns

### Backward compatibility

All three gaps are additive. No existing `mcp.config.yaml` file needs modification.
New config fields have defaults that preserve current behaviour. The `--watch` flag
defaults to `false`. OAuth is only activated when `type: oauth2_client_credentials` is
present in the config.

### Security review deltas

| Area | v1.0 | v1.1 delta |
|---|---|---|
| Credential storage | `SensitiveStr` in `ResolvedScheme` | OAuth `access_token` in `CachedToken` (not wrapped in `SensitiveStr` in v1.1 — tracked as follow-up) |
| Credential logging | Never logged | OAuth token must not appear in `_fetch_token` error messages — review required |
| Network calls in auth path | None | `_fetch_token` makes an outbound HTTPS call; `trust_env=False` must be set |
| File watching | N/A | `watchfiles` does not execute file contents; low risk |

### Dependencies added

| Package | Gap | Justification |
|---|---|---|
| `watchfiles>=0.21` | `--watch` | Async-native, used by uvicorn/FastAPI, actively maintained |

No new dependencies for OAuth or SSE — `httpx` is already in the dependency tree.

### Test infrastructure

The existing `respx` mock library handles all three gaps:
- `--watch`: no HTTP mocking needed (file system only)
- OAuth: `respx.post(token_url).mock(...)` for the token endpoint
- SSE: `respx` supports streaming response mocks via `httpx.Response` with iterator content

The existing anyio memory stream E2E harness in `test_serve_e2e.py` can be extended
for all three gaps without structural changes.

---

## What is explicitly deferred to v1.2+

| Feature | Reason for deferral |
|---|---|
| OAuth authorization_code flow | Requires browser redirect URI; not meaningful for stdio MCP |
| OAuth token refresh_token flow | Builds on client_credentials; add after v1.1 validates the cache architecture |
| True MCP streaming (async generator handler) | Requires MCP SDK changes outside specmcp's control |
| WebSocket upstream support | Different protocol; narrow API surface |
| HTTP transport for MCP server | Stub exists in serve.py; complete after stdio is stable |
| `access_token` wrapped in `SensitiveStr` | Minor hardening; low priority once logging is reviewed |
