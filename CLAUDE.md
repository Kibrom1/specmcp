# specmcp — AI Agent Guide

This file gives Claude (and other AI coding assistants) a quick map of the codebase so you can navigate it confidently without reading every file.

---

## What the project does

**specmcp** converts any OpenAPI spec into a working MCP (Model-Context Protocol) server with no code. It runs a load → normalize → simplify → expose pipeline at startup, then answers `tools/list` and `tools/call` MCP requests by proxying the real upstream API.

---

## Source layout

```
src/specmcp/
├── cli/              — Typer CLI commands (entry point: specmcp.cli:app)
│   ├── app.py        — Typer app singleton
│   ├── serve.py      — `serve` command (the main server)
│   ├── init.py       — `init` command (scaffold mcp.config.yaml)
│   ├── inspect.py    — `inspect` command (dump normalised ops)
│   └── validate.py   — `validate` command (lint the spec)
├── core/             — Pure pipeline stages (no I/O)
│   ├── load.py       — parse + resolve OpenAPI spec (prance)
│   ├── normalize.py  — canonical Operation list
│   ├── simplify.py   — LLM-friendly SimplifiedOperation + arg map
│   └── expose.py     — ToolRegistry (name → ToolDefinition)
├── auth/             — Authentication layer
│   ├── injector.py   — AuthInjector: injects auth per-scheme into requests
│   ├── token_store.py — TokenStore ABC + InMemoryTokenStore + SqliteTokenStore
│   ├── oauth2_authcode.py — AuthCodeHandler: per-scheme OAuth2 PKCE flow
│   ├── encryption.py — AES-256-GCM + HKDF helpers (for SqliteTokenStore)
│   ├── login_nonce.py — LoginNonceStore: short-lived login tokens
│   ├── pkce.py       — PKCE verifier/challenge generation (RFC 7636)
│   ├── state.py      — OAuth state/PKCE TTL cache helpers
│   └── token_cache.py — Per-session token refresh TTL cache
├── runtime/          — Async runtime (runs inside anyio)
│   ├── dispatcher.py — dispatch(): resolve args → inject auth → call API
│   ├── http_client.py — HttpClient (httpx async, connection pooling)
│   ├── oauth_handler.py — Starlette OAuth HTTP endpoints + OAuthHandlerState
│   ├── registry_ref.py — RegistryRef (atomic hot-swap for --watch)
│   └── session.py    — SessionContext (per-connection identity + token)
├── config.py         — Pydantic Config model (mcp.config.yaml)
└── errors.py         — SpecmcpError hierarchy + helpers
```

---

## Key data-flow

```
specmcp serve --spec api.yaml
  │
  ├─ load_spec() → normalize() → simplify() → ToolRegistry
  ├─ AuthInjector.build(cfg)          — resolves credentials at startup
  ├─ [HTTP] _build_oauth_state()      — builds OAuthHandlerState, registers AuthCodeHandlers
  │
  │  [per tool call]
  └─ dispatcher.dispatch()
       ├─ auth_injector.inject(tool, session) → adds auth headers/params
       └─ http_client.request()               → upstream API → MCP content blocks
```

---

## `serve` CLI flags reference

| Flag | Default | Purpose |
|------|---------|---------|
| `--spec` / `-s` | *(from config)* | Path or URL to OpenAPI spec |
| `--config` / `-c` | `mcp.config.yaml` | Config file path |
| `--transport` / `-t` | `stdio` | `stdio` or `http` |
| `--watch` / `-w` | `False` | Hot-reload spec/config on change |
| `--verbose` / `-v` | `False` | DEBUG-level logging |
| `--management-port` | *(from config, default 8766)* | Override management endpoint port |
| `--management-bind` | *(from config, default `loopback`)* | `loopback` or `all` |
| `--token-store` | `memory` | `memory` (lost on restart) or `sqlite` (encrypted at rest) |
| `--token-store-path` | `~/.specmcp/tokens.db` | SQLite database path (sqlite only) |
| `--token-store-key-env` | `SPECMCP_TOKEN_KEY` | Env var name holding encryption key (sqlite only) |

---

## OAuth2 Authorization Code + PKCE flow

OAuth support is wired together from several modules:

### Core modules

**`auth/pkce.py`** — `generate_verifier() → str`, `generate_challenge(verifier) → str` (S256, RFC 7636).

**`auth/login_nonce.py`** — `LoginNonceStore`: issues and consumes short-lived login nonces (UUID + TTL). Nonces bind a session to an upcoming OAuth flow.

**`auth/state.py`** — `OAuthStateStore` (TTL dict mapping `state` → `(verifier, scheme_name)`). The `scheme_name` is stored alongside the PKCE verifier so the callback can route tokens to the correct scheme without a guessing fallback.

**`auth/oauth2_authcode.py`** — `AuthCodeHandler`: per-scheme handler called by the injector on every tool call.
- Checks the token store; injects `Authorization: Bearer <token>` if valid.
- Silent refresh via `POST token_url` with `grant_type=refresh_token` (HTTP Basic Auth, RFC 6749 §2.3.1).
- If no valid token: issues a login nonce and raises `AuthRequiredError(login_url=...)`.
- Per-session `asyncio.Lock` (TTL-cached) prevents concurrent double-refresh races.

**`runtime/oauth_handler.py`** — Starlette route handlers for:
- `GET  /auth/login?nonce=<token>` — consumes nonce, generates PKCE, redirects to IdP
- `GET  /auth/callback?code=&state=` — exchanges code for tokens, stores via `scheme_name` tag
- `GET  /auth/status?session=<id>` — poll for `{"authenticated": true|false}`
- `DELETE /auth/session/<id>` — revoke/delete tokens (management endpoint, loopback-only by default)

**`OAuthHandlerState`** (in `oauth_handler.py`) — runtime state shared across all OAuth routes:
- `schemes: dict[str, ResolvedAuthCodeScheme]` — one entry per configured auth code scheme
- `LoginNonceStore` — shared nonce store
- PKCE state store (TTL cache via `auth/state.py`)
- `server_secret` — HMAC key for signed state tokens
- `management_bind_all`, `management_token` — access control for management endpoints

### Wiring in `serve.py`

`_build_oauth_state(cfg, auth_injector, login_base_url, *, token_store_type, sqlite_db_path, sqlite_key_bytes)`:
1. Finds all `OAuth2AuthorizationCodeConfig` schemes in `cfg`.
2. Resolves `client_id` / `client_secret` from env vars.
3. Creates one token store per scheme (`InMemoryTokenStore` or `SqliteTokenStore`).
4. Builds `OAuthHandlerState`.
5. Creates `AuthCodeHandler` per scheme and registers via `auth_injector.register_auth_code_handler()`.
6. Returns the `OAuthHandlerState` (or `None` if no auth code schemes present).

`_run_http()` then:
- Calls `await store.open()` for each scheme's token store before serving.
- Mounts OAuth routes via `build_oauth_routes(oauth_state)`.
- Calls `await store.close()` in a `finally` block on shutdown.

### Token store lifecycle

`TokenStore` ABC defines `open()` and `close()` with no-op defaults so all store types can be treated uniformly. `SqliteTokenStore` overrides both to open/close the `aiosqlite` connection.

---

## Auth injection (`auth/injector.py`)

`AuthInjector` holds one `ResolvedScheme` per configured scheme. On `inject(tool, session)`:
- For `ApiKeyAuthConfig` / `BearerAuthConfig` / `OAuth2ClientCredentialsConfig`: injects directly from the resolved credential.
- For `OAuth2AuthorizationCodeConfig`: delegates to the registered `AuthCodeHandler.apply(headers, params, session=session)`, which may raise `AuthRequiredError`.

**Important**: `inject()` returns new `(headers, params)` dict copies — it never mutates the caller's dicts.

`AuthRequiredError` is caught in `serve.py`'s `handle_call_tool` and formatted via `mcp_error_content()` as a text MCP error response containing the `login_url` so the client knows where to send the user.

---

## Configuration (`config.py`)

Key models:
- `Config` — top-level; loaded from `mcp.config.yaml`
- `SpecConfig` — `source` (path or URL)
- `TransportConfig` / `HttpTransportConfig` — host/port for HTTP transport
- `ManagementConfig` — `bind` (`loopback`|`all`), `port` (default 8766), `management_token_from`
- `AuthSchemeConfig` — union of `ApiKeyAuthConfig`, `BearerAuthConfig`, `OAuth2ClientCredentialsConfig`, `OAuth2AuthorizationCodeConfig`
- `SimplifyConfig`, `DispatchConfig`, `ServerConfig` — pipeline tuning

`_resolve_value_from(spec, context)` — resolves `env(VAR)` / `literal(val)` directives into strings at startup.

---

## Testing

Tests live under `tests/unit/`. Run with `pytest`.

Key test files:
- `tests/unit/test_serve.py` — CLI smoke, `_build_oauth_state`, `_run_http` route mounting, management + token-store flags
- `tests/unit/runtime/test_oauth_handler.py` — all OAuth HTTP endpoint unit tests
- `tests/unit/runtime/test_oauth_e2e.py` — end-to-end OAuth flow (nonce → login → callback → status → inject)
- `tests/unit/auth/test_oauth2_authcode.py` — `AuthCodeHandler` unit tests
- `tests/unit/runtime/test_auth_code_dispatch.py` — injector + dispatch integration

The `respx` library mocks outbound `httpx` calls (token endpoint, upstream API). Starlette's `TestClient` is used for HTTP handler tests (synchronous ASGI client).

**Gotcha — `inject()` return value**: `AuthInjector.inject()` returns new dict copies. Capture the return:
```python
headers, params = await injector.inject(tool, session=session)
# NOT: await injector.inject(...); then use the original `headers` dict
```

**Gotcha — `_check_management_access` in `TestClient`**: Starlette `TestClient` sets the client host to `"testclient"`, which fails the loopback check. Patch `specmcp.runtime.oauth_handler._check_management_access` to `return True` in tests that hit management endpoints.

---

## Common patterns

### Adding a new auth scheme type

1. Add the config model to `config.py` (follow `ApiKeyAuthConfig`).
2. Add an `inject_<scheme>` branch to `AuthInjector._inject_scheme()`.
3. Add the new config to the `AuthSchemeConfig` union type alias.
4. Write tests in `tests/unit/test_auth.py`.

### Adding a new CLI flag

1. Add the `typer.Option` parameter to `serve_cmd` in `cli/serve.py`.
2. Validate the value early (before the pipeline runs).
3. Thread the resolved value down through `anyio.run → _run_server → _run_http / _build_oauth_state` as needed.
4. Add tests to `tests/unit/test_serve.py` (use `runner.invoke` + `patch("specmcp.cli.serve.anyio.run")`).

### Hot-reload (`--watch`)

`_watch_and_reload` monitors spec + config files via `watchfiles.awatch`. On change it calls `_run_pipeline()` and atomically swaps the `RegistryRef`. The `AuthInjector` is **not** rebuilt — auth changes require a full restart.
