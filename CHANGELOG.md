# Changelog

All notable changes to specmcp are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed

- **`specmcp init` OAuth2 scaffold** — `init` now correctly inspects the spec's
  `flows` field to distinguish `oauth2_authorization_code` from
  `oauth2_client_credentials` schemes (previously both fell through to an
  unsupported-type commented stub). Env-var names are now generated as
  `SCHEME_CLIENT_ID` / `SCHEME_CLIENT_SECRET` for all OAuth2 schemes.
  `Config.scaffold()` emits a full YAML block for each type, and outputs
  `version: "2"` when any authorization-code scheme is present.

### Added

- **`specmcp validate` tool count** — `validate` now runs the full pipeline
  (load → normalize → simplify → expose) and reports `Tools exposed: N`
  alongside `Operations found: N`. The `--json` output gains `tool_count`,
  `hidden_count`, `fallback_count`, `simplify_warning_count`, and
  `auth_schemes` fields.
- **`specmcp inspect --json` `hidden_count`** — the `--json` output now
  includes a `hidden_count` field (operations filtered out by config) to
  match the human-readable summary.
- **`specmcp report-issue` filing hint** — the command now prints the GitHub
  issues URL to stderr after emitting the JSON report, making it easier to
  file issues without reading the docs.
- **Nightly integration test** — `tests/integration/test_petstore_nightly.py`
  fetches the live Petstore spec and asserts pipeline invariants (tool count,
  unique names, valid schemas). Runs automatically via the new
  `.github/workflows/nightly.yml` workflow (03:00 UTC daily) and can be
  triggered manually or locally with `SPECMCP_NIGHTLY=1`.
- **`CONTRIBUTING.md`** — developer guide covering setup, testing, code style,
  how to add auth scheme types and CLI commands, and the PR checklist.

---

## [1.3.0] — 2026-05-28

### Added

**Dedicated management listener** (`--management-port`)

The `DELETE /auth/session/<id>` management endpoint now runs on its own dedicated
port (default 8766) instead of sharing the main HTTP transport port. This separates
public-facing OAuth routes (`/auth/login`, `/auth/callback`, `/auth/status`) from the
privileged session-revocation endpoint.

Two Starlette apps are now started in an inner anyio task group when OAuth
authorization-code schemes are configured:

- **Main app** (default port 8765): `/sse`, `/messages`, `/auth/login`,
  `/auth/callback`, `/auth/status`.
- **Management app** (default port 8766): `DELETE /auth/session/<id>` only,
  bound to `127.0.0.1` (loopback) by default or `0.0.0.0` when
  `--management-bind all` is set.

The `--management-port` flag now has full routing effect (the "no routing effect"
warning previously emitted at startup has been removed).

### Changed

- `build_oauth_routes()` in `runtime/oauth_handler.py` is preserved for backward
  compatibility. Two new helpers, `build_public_oauth_routes()` and
  `build_management_routes()`, are used internally by `_run_http` to populate the
  respective apps.
- `CLAUDE.md` updated to remove the "reserved" status for `--management-port` and
  to describe the two-listener architecture.

---

## [1.2.0] — 2026-05-28

### Added

**OAuth 2.0 Authorization Code + PKCE flow** (`oauth2_authorization_code`)

Full interactive login support for APIs that require user-delegated OAuth (GitHub,
Google, Salesforce, etc.). The server issues a short-lived login URL to the LLM when
a session has no valid token; the user visits the URL, authenticates, and subsequent
tool calls proceed automatically with the stored access token.

Key properties:
- PKCE (RFC 7636, S256) protects every authorization request.
- Silent token refresh via `refresh_token` grant (RFC 6749 §4.1.4) before expiry.
  Per-session `asyncio.Lock` prevents concurrent double-refresh races.
- Tokens are stored either in memory (`--token-store memory`, default) or in an
  AES-256-GCM encrypted SQLite database (`--token-store sqlite`).
- Multi-scheme configs create one database file per scheme beside the base path
  (e.g. `~/.specmcp/tokens_myScheme.db`).

```yaml
auth:
  myApi:
    type: oauth2_authorization_code
    authorization_url: https://auth.example.com/oauth/authorize
    token_url: https://auth.example.com/oauth/token
    redirect_uri: https://yourserver.example.com/auth/callback
    client_id_from: env(MY_API_CLIENT_ID)
    client_secret_from: env(MY_API_CLIENT_SECRET)
    scopes:
      - read
      - write
```

**HTTP transport** (`specmcp serve --transport http`)

Runs the MCP server over HTTP/SSE instead of stdio. Required for OAuth authorization
code flows (which need an HTTP callback endpoint for the IdP redirect). Defaults to
port 8765 and `localhost`.

**OAuth management endpoints** (HTTP transport only)

Four routes for the OAuth lifecycle (public routes on the main app; management on
a dedicated port — see [1.3.0]):

| Route | Purpose |
|---|---|
| `GET /auth/login?nonce=<token>` | Redirects the user to the IdP authorization page |
| `GET /auth/callback?code=&state=` | Exchanges the authorization code for tokens |
| `GET /auth/status?session=<id>` | Polls authentication state (`{"authenticated": true\|false}`) |
| `DELETE /auth/session/<id>` | Revokes a session's stored tokens |

`DELETE /auth/session/<id>` is a management endpoint: accessible from loopback only
by default; set `--management-bind all` and `management.management_token_from` to
expose it externally with Bearer auth.

**`--token-store` flag** (`memory` | `sqlite`)

Controls where OAuth tokens are persisted across tool calls:

- `memory` (default) — tokens are lost on server restart.
- `sqlite` — tokens are encrypted at rest with AES-256-GCM + HKDF and stored in a
  SQLite file. Use with `--token-store-path` and `--token-store-key-env`.

**`--management-bind` and `--management-port` flags**

`--management-bind loopback` (default) restricts the `DELETE /auth/session/<id>`
endpoint to loopback addresses only. `--management-bind all` opens it to all
interfaces and requires a Bearer token (set via `management.management_token_from`
in config).

`--management-port` sets the port for the dedicated management listener (default
8766). See [1.3.0] for the two-listener architecture that routes this endpoint to
its own Starlette app.

**`scripts/token_store_rotate.py`** — key rotation utility

Re-encrypts all rows in an SQLite token store with a new AES-256-GCM key. The
rotation is atomic (writes a temp copy, replaces original only on full success).
For multi-scheme configs, run the script once per scheme file.

```sh
python scripts/token_store_rotate.py \
  --db ~/.specmcp/tokens_myScheme.db \
  --old-key <64-hex-chars> \
  --new-key <64-hex-chars>
```

### Security

- **XSS in OAuth callback error page**: the `error` query parameter returned by the
  IdP is now HTML-escaped via `html.escape()` before insertion into the error page.
  A `Content-Security-Policy: default-src 'none'` header is also set as defence-in-depth.
- **IPv4-mapped loopback**: the management endpoint loopback allowlist now includes
  `::ffff:127.0.0.1` (the IPv4-mapped loopback address on dual-stack Linux hosts)
  alongside `127.0.0.1`, `::1`, and `localhost`.
- **`redirect_uri` HTTPS enforcement**: `OAuth2AuthorizationCodeConfig` now applies
  the same HTTPS validator as `authorization_url` and `token_url`. `http://` URIs are
  rejected unless the host is `localhost` or `127.0.0.1`.
- **`AuthRequiredError` with `login_url=None`**: when nonce issuance fails, the MCP
  error content previously rendered the literal string `"None"`. It now returns a
  coherent fallback message directing the user to check server logs.
- **`SPECMCP_TOKEN_KEY` entropy warning**: keys shorter than 16 bytes emit an advisory
  to stderr at startup. AES-256-GCM + HKDF derivation still functions with short keys
  but with reduced security margin.

### Changed

- `TokenStore` ABC gains no-op `open()` and `close()` defaults so all store types
  can be lifecycle-managed uniformly. `SqliteTokenStore` overrides both to manage
  the `aiosqlite` connection.
- `CachedToken.access_token` is now stored as `SensitiveStr` (was plain `str`).
  Accidental `repr()` / `str()` calls on `CachedToken` no longer expose the raw
  token value in logs. `get_or_refresh()` still returns a plain `str` via
  `reveal()` so the public API is unchanged.
- Dispatcher retries on 401 for `oauth2_client_credentials` schemes: when the
  upstream returns HTTP 401, any cached access token is invalidated and the
  request is retried once with a freshly-fetched token. A second 401 raises
  `AuthError` instead of `UpstreamClientError` so the LLM receives a clear
  "credentials rejected" message rather than a raw upstream error. Bearer,
  API-key, and authorization-code schemes are not retried (a 401 there means
  the credential is wrong, not stale).
- `CLAUDE.md` updated with OAuth flow walkthrough, security invariants section, and
  serve CLI flags reference table.

---

## [1.1.0] — 2026-05-28

### Added

**`--watch` mode** (`specmcp serve --watch` / `-w`)

Hot-reloads the ToolRegistry when the spec or config file changes on disk,
without dropping the stdio MCP connection. Useful during development when
iterating on a spec or adjusting tool filters. Requires `watchfiles` (`pip
install watchfiles`). Changes to the `auth:` section of `mcp.config.yaml`
are not picked up on reload — a full restart is required for auth changes.
The watcher emits an explicit warning to stderr when the config file changes
so this limitation is always visible.

**OAuth 2.0 client_credentials flow**

New `oauth2_client_credentials` auth scheme type in `mcp.config.yaml`.
specmcp exchanges your `client_id` and `client_secret` for a short-lived
access token at the configured `token_url`, caches it in memory, and
refreshes it automatically before expiry (60-second buffer). Concurrent
tool calls share one in-flight refresh (thundering-herd prevention). The
`specmcp init` scaffold now emits a full `oauth2_client_credentials` stub
for APIs that declare OAuth2 security schemes.

```yaml
auth:
  myApi:
    type: oauth2_client_credentials
    token_url: https://auth.example.com/oauth/token
    client_id_from: env(MY_API_CLIENT_ID)
    client_secret_from: env(MY_API_CLIENT_SECRET)
    scopes:
      - read
      - write
```

**SSE / streaming response support**

Operations that declare a `text/event-stream` response are now handled by
a dedicated streaming path rather than a buffered request. `data:` lines
are collected and delivered to the LLM as a single text block after the
stream closes (or after `[DONE]`). This unblocks APIs like OpenAI chat
completions and Anthropic streaming that previously caused an indefinite
hang or `ResponseTooLargeError`. Three new `dispatch:` config fields:

| Field | Default | Description |
|---|---|---|
| `enable_streaming` | `true` | Set to `false` to always buffer (useful for debugging) |
| `streaming_timeout_multiplier` | `5.0` | Multiplied by the resolved timeout for SSE calls. A 30s timeout becomes 150s. Note: per-operation `timeout_seconds` overrides are also multiplied. |
| `streaming_max_bytes` | `4194304` (4 MiB) | Truncates runaway streams; appends `[Response truncated]` |

### Changed

- `specmcp init` scaffold now includes `enable_streaming`,
  `streaming_timeout_multiplier`, and `streaming_max_bytes` in the
  `dispatch:` section with explanatory comments.
- `--verbose` / `-v` on `specmcp serve` now enables DEBUG-level logging,
  printing each tool call, auth injection step, and outbound HTTP request.
  Previously the flag was accepted but had no effect.
- `--transport` help text now accurately describes `http` transport as
  planned for a future release rather than a current option.
- `--watch` help text corrected: URL specs emit a warning to stderr rather
  than being silently ignored.

### Fixed

- `specmcp init` generated incorrect environment variable names for OAuth
  scheme names containing a single uppercase letter before a title-cased
  word (e.g. `myOAuth` → `MY_O_AUTH_CLIENT_ID` instead of
  `MY_OAUTH_CLIENT_ID`). The camelCase boundary regex now requires two or
  more consecutive uppercase letters before splitting, matching the natural
  expectation for initialisms like `OAuth`, `URL`, and `ID`.

---

## [1.0.0] — initial release

- Load → Normalize → Simplify → Expose pipeline converts any OpenAPI 3.x
  spec into a working MCP server with no code generation.
- Auth: `apiKey` (header, query, cookie) and `bearer` schemes.
- HTTP client with timeout, retry, response size guard, and text truncation.
- `specmcp init` scaffolds `mcp.config.yaml` and `.env.example`.
- `specmcp inspect` lists all exposed tools without starting the server.
- `specmcp validate` checks the spec and config and exits with a status code.
- `specmcp report-issue` bundles a sanitized debug report for filing issues.
- stdio MCP transport; per-operation overrides (rename, description, hide,
  timeout, retry, server URL).
