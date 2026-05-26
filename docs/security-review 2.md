# specmcp v0.1.0 Security Review

**Date:** 2026-05-20  
**Scope:** `src/specmcp/` — auth, runtime, CLI, config  
**Status:** No critical issues found.

---

## Findings

### ✅ PASS — Credential isolation (SensitiveStr)

All auth credentials are stored as `SensitiveStr` from the moment they are read
from environment variables. `SensitiveStr.__repr__` and `__str__` both return
`"<redacted>"`, so credentials cannot appear in:
- Exception messages
- Log output
- `repr()` of any object holding a credential
- `inspect --json` or `report-issue` output

`SensitiveStr.reveal()` is called in exactly one place:
`src/specmcp/auth/injector.py:166` — immediately before writing the value into
an outbound HTTP header or query parameter. No other call site exists.

### ✅ PASS — No shell injection

No `subprocess`, `os.system`, `eval()`, or `exec()` calls exist in the codebase.
All external interaction is through httpx (HTTP) and prance (spec resolution).

### ✅ PASS — Request headers not included in exceptions

HTTP request headers (which carry auth tokens after injection) are never captured
in exception messages. `TransientError`, `UpstreamClientError`, and
`UpstreamServerError` carry only: status code, message, and request_id.

### ✅ PASS — Response body bounded and not in error messages

Response bodies are bounded at `response_size_limit_bytes` (default 1 MiB) before
decoding. Upstream error responses (4xx) store the body on `UpstreamClientError.body`
but this field is:
- Not included in `str(exc)`, `exc.to_dict()`, or `mcp_error_content(exc)`
- Not logged anywhere in the current codebase
- Only accessible to callers who explicitly read `exc.body`

MCP error content for `UpstreamClientError` is:
`"Upstream returned HTTP {status_code}: {message}"` — no body.

**Note:** `exc.body` may contain PII from upstream APIs. If structured logging is
added in a future version, take care not to include `exc.body` in log records.

### ✅ PASS — TLS verification on by default

`DispatchConfig.tls_verify` defaults to `True`. Disabling TLS verification requires
an explicit opt-in in `mcp.config.yaml`:
```yaml
dispatch:
  tls_verify: false   # only for local dev/testing; never in production
```

### ✅ PASS — No credential values in config summary or report-issue

`specmcp report-issue` calls `_summarize_config()` which reads `scheme.value_from`
(e.g. `"env(PETSTORE_API_KEY)"`) — the env var *name*, not the resolved value.
`Config.resolve_auth_values()` is never called in the report-issue command.

### ⚠️ NOTE — Path traversal not sanitized (acceptable for CLI)

Spec source paths and config file paths are passed directly to `Path()` / prance.
No path traversal sanitization is applied. This is acceptable because:
1. specmcp is a CLI tool run by the user with their own file system access.
2. The spec source is user-supplied (they could point it anywhere they choose).
3. A server-mode hardening pass (M5+) should add an allowlist if specmcp is ever
   exposed as a service.

### ⚠️ NOTE — Proxy environment variables disabled

The HTTP client uses `trust_env=False` in `httpx.AsyncClient` to prevent the
sandbox's `HTTP_PROXY` / `SOCKS_PROXY` env vars from being used. This means
specmcp currently ignores system proxy settings. If proxy support is needed, it
should be added as an explicit `dispatch.proxy_url` config option (never from env,
to avoid SSRF via proxy manipulation).

---

## Auth error messaging

Auth errors shown to the LLM are intentionally generic:

| Error | LLM sees |
|---|---|
| `AuthConfigError` (scheme not configured) | `Authentication failed (request_id: ...)` |
| `AuthError` (upstream rejected credentials) | `Authentication failed (request_id: ...)` |

The operator can correlate `request_id` with server-side logs to diagnose without
exposing credential details to the LLM.

---

## Recommendations for future versions

1. **Structured logging** — When adding structlog event emission, never include
   `exc.body`, auth headers, or credential values in log records.
2. **Rate limiting** — Add per-tool call-rate limits to prevent an LLM agent from
   exhausting upstream API quotas.
3. **Input schema validation** — The dispatcher currently trusts that the MCP SDK
   has validated args against the tool's `inputSchema`. Add an explicit jsonschema
   validation step before dispatch for defence in depth.
4. **Proxy support** — If added, implement as explicit config rather than env var
   passthrough to prevent SSRF via proxy manipulation.
