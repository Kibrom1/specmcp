# Changelog

All notable changes to specmcp are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.1.0] — unreleased

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
