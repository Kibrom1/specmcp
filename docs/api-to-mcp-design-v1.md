# Engineering Design: specmcp (v1)

**Status:** Draft v0.4 (architecture extension — error model, dispatcher, config schema, worked example)
**Scope:** v1 only (OpenAPI 3 → MCP proxy server, API key + Bearer auth, macOS/Linux)
**Upstream doc:** `api-to-mcp-requirements.md` v0.3

---

## 1. Purpose

This document describes how we will build v1. It covers architecture, key components, data flow, technology choices, and the design decisions that aren't obvious from the requirements. It does not re-derive the requirements; for the "what" and "why," see the upstream doc.

## 2. Design Principles

These guide the trade-offs throughout:

1. **Spec-driven, not code-driven.** The OpenAPI document is the runtime source of truth. No hardcoded API knowledge.
2. **Fail loud at startup, gracefully at runtime.** Spec errors surface at `serve` / `validate` time; runtime errors are caught and returned as MCP errors.
3. **Deterministic outputs.** Same spec + same config → same exposed tools, every time. No clever heuristics that drift.
4. **Composable internals.** The pipeline (parse → normalize → simplify → expose) is a sequence of pure-ish stages so v1.1 codegen can reuse the same front end.
5. **Don't trust the spec.** Real-world OpenAPI is messy. Every stage validates and reports rather than assumes.

## 3. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                          CLI Layer                            │
│   init  │  validate  │  inspect  │  serve  │  report-issue   │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                       Pipeline (shared)                       │
│                                                               │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐  │
│  │  Load   │──▶│ Normalize│──▶│ Simplify │──▶│   Expose   │  │
│  │ + Parse │   │ (refs,   │   │ (LLM-    │   │ (tool      │  │
│  │         │   │  v3.0/   │   │  usable  │   │  registry) │  │
│  │         │   │  v3.1)   │   │  schemas)│   │            │  │
│  └─────────┘   └──────────┘   └──────────┘   └────────────┘  │
│       ▲              ▲              ▲              ▲          │
│       │              │              │              │          │
│       └──────────────┴──────┬───────┴──────────────┘          │
│                             │                                 │
│                       Config + Overrides                      │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                        MCP Server Runtime                     │
│                                                               │
│   MCP Protocol Handler  ◀──▶  Tool Dispatcher                 │
│            │                          │                       │
│            │                          ▼                       │
│            │                  ┌───────────────┐               │
│            │                  │  HTTP Client  │               │
│            │                  │  (auth, retry,│               │
│            │                  │   rate limit) │               │
│            │                  └───────────────┘               │
│            │                          │                       │
│            ▼                          ▼                       │
│       MCP Client                Upstream API                  │
└──────────────────────────────────────────────────────────────┘
```

The pipeline is shared between CLI commands and the server. `validate` runs Load + Normalize. `inspect` runs the full pipeline and prints. `serve` runs the full pipeline and starts the runtime.

## 4. Component Design

### 4.1 CLI Layer

**Tech:** Python 3.11+, CLI framework: `typer` (built on Click, type-hint-driven, good DX, mature).

**Why Python:**
- Team familiarity and preference.
- The official MCP Python SDK (`mcp`) is well-maintained.
- Python's async stack (`asyncio`, `httpx`, `anyio`) is solid for an I/O-bound proxy.
- v1.1 codegen output can target any language regardless of what the converter itself is written in.

**Trade-offs being accepted:**
- The OpenAPI ecosystem is slightly thinner in Python than JS/TS. We mitigate with `openapi-core` for validation and `prance` for ref resolution (covered below).
- Single-binary distribution requires more work than Node (`pyinstaller` / `shiv` / `pex` rather than `pkg`). See §7.
- Startup time is higher than Node. Budget in §9 accounts for this.

**Commands and their pipelines:**

| Command | Pipeline stages | Output |
|---|---|---|
| `init` | Load → Normalize | Writes `mcp.config.yaml` scaffold |
| `validate` | Load → Normalize | Exit code 0/non-0; errors with file:line |
| `inspect` | Load → Normalize → Simplify → Expose | Human-readable or `--json` listing of tools |
| `serve` | Full pipeline + Runtime | Runs MCP server on stdio (default) or HTTP |
| `report-issue` | Load → Normalize → sanitize | Writes a redacted bundle to disk |

All commands accept `--config <path>`, `--spec <path-or-url>`, `--verbose`, `--json`.

### 4.2 Load + Parse

**Responsibilities:**
- Fetch spec from local path or URL.
- Detect format (JSON vs YAML, OpenAPI vs Swagger 2).
- Parse to in-memory AST.
- Surface syntax errors with line numbers.

**Tech:**
- `prance` for parsing and `$ref` resolution. Prance handles external refs, circular refs, and supports both YAML and JSON.
- `openapi-spec-validator` for meta-schema validation against the OpenAPI 3.0/3.1 specifications.
- `ruamel.yaml` (not PyYAML) for raw YAML with line/column tracking, used only for error reporting.
- `httpx` for fetching remote specs.

**Decisions:**
- We keep both a "raw" view (with line info from ruamel) and a "resolved" view (with refs inlined via prance). Error messages reference the raw view; downstream stages use the resolved view.
- Swagger 2.0 input is **out of scope for v1** (deferred to v1.1). Detect it and emit a clear "not yet supported, planned for v1.1" error rather than half-supporting.
- Circular refs: keep them as cycles in the resolved view but flag them; the Simplify stage breaks them deterministically.
- We pin prance to a known-good version and add wrapper functions so we can swap to `jsonref` or a custom resolver if we hit limits.

### 4.3 Normalize

**Responsibilities:**
- Reduce OpenAPI 3.0 and 3.1 to a single internal representation (the **Operation Model**).
- Resolve servers/base URLs.
- Collapse parameter inheritance (path-level + operation-level).
- Apply config-level overrides (rename, hide, regroup).

**Operation Model (Pydantic):**

The model is exhaustive for v1's HTTP/OpenAPI surface. It is the single canonical representation consumed by all downstream stages; every shape OpenAPI can express must round-trip into it without loss of dispatch-relevant information.

```python
from pydantic import BaseModel, Field
from typing import Literal, Any

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
ParamLocation = Literal["path", "query", "header", "cookie"]
ParamStyle = Literal[
    "simple", "label", "matrix",          # path
    "form", "spaceDelimited", "pipeDelimited", "deepObject",  # query
]

class Parameter(BaseModel):
    name: str
    location: ParamLocation
    required: bool
    schema_: dict[str, Any]               # JSON Schema 2020-12
    style: ParamStyle | None = None       # OpenAPI serialization style
    explode: bool | None = None           # OpenAPI explode flag
    allow_empty_value: bool = False
    deprecated: bool = False

class RequestBodyVariant(BaseModel):
    """One content-type representation of the body."""
    content_type: str                     # "application/json", "multipart/form-data", etc.
    schema_: dict[str, Any]
    encoding: dict[str, dict] | None = None  # per-field encoding for multipart/form

class RequestBody(BaseModel):
    required: bool
    variants: list[RequestBodyVariant]    # may be >1 when spec offers multiple content types
    # Dispatch rule: pick variants[0] unless overridden in config.

class ResponseVariant(BaseModel):
    """One content-type representation of a response."""
    content_type: str
    schema_: dict[str, Any] | None = None
    is_binary: bool = False               # signals MCP resource vs text mapping

class Response(BaseModel):
    status_code: str                      # "200", "2XX", "default"
    description: str | None = None
    headers: dict[str, dict] = Field(default_factory=dict)  # for future use
    variants: list[ResponseVariant] = Field(default_factory=list)

class AuthRequirement(BaseModel):
    """Reference to a securityScheme by name + the scopes the operation requires."""
    scheme_name: str
    scopes: list[str] = Field(default_factory=list)

class Operation(BaseModel):
    id: str                               # stable, derived; collisions resolved deterministically
    method: HttpMethod
    path: str                             # e.g. "/users/{id}"
    server_url: str                       # resolved, no server variables
    server_variables_resolved: dict[str, str] = Field(default_factory=dict)
    parameters: list[Parameter]           # path+operation-level merged, no dupes
    request_body: RequestBody | None = None
    responses: list[Response]             # ordered: 2xx first, then others, default last
    auth: list[list[AuthRequirement]]     # outer list = OR, inner list = AND (OpenAPI semantics)
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False
    source_location: tuple[str, int]      # (file, line) for error messages
    vendor_extensions: dict[str, Any] = Field(default_factory=dict)  # x-* preserved for inspect/codegen
```

**Decisions and edge-case rules:**

- **Pydantic v2** for the model. Validation at stage boundaries catches bugs early.
- **OpenAPI 3.0 vs 3.1.** Normalize forward to 3.1 / JSON Schema 2020-12. `nullable: true` becomes `type: [..., "null"]`. `exclusiveMaximum: true` (3.0 boolean) becomes `exclusiveMaximum: <value>` (3.1 numeric). `example` (3.0) is preserved alongside `examples` (3.1).
- **Operation IDs.** Prefer `operationId`. Fallback: `{method_lower}_{sanitized_path}` where sanitization is: lowercase, `/` → `_`, strip `{}`, collapse `__` to `_`, strip leading/trailing `_`. Collisions resolved by appending `_2`, `_3`, ... in spec order. The algorithm is documented in `docs/naming.md` and must not change without a deprecation cycle.
- **Multiple servers.** v1 picks `servers[0]`. Per-operation `servers` override the global. Config can override either. Server variables (`{host}.example.com`) are resolved at load time from variable defaults or config; unresolved variables are a startup error.
- **Multiple request body content types.** v1 picks `variants[0]` deterministically (spec order). Config can pin a different variant per operation. If the LLM needs to send a different content type than the chosen variant, that's a v2 feature.
- **Response variants.** All variants are kept in the model. The dispatcher picks the variant matching the upstream `Content-Type` header at runtime. If the upstream returns a content type not in the spec, we still pass through the response (don't refuse) but log a warning.
- **`oneOf` / `anyOf` / `allOf` in schemas.** Preserved as-is in `schema_` dicts. Resolution into LLM-usable shapes is Simplify's problem (§4.4); the Operation Model holds the raw shape.
- **Form-encoded bodies (`application/x-www-form-urlencoded`).** Same shape as JSON in the model; the dispatcher encodes differently at request time.
- **`multipart/form-data`.** `encoding` field on `RequestBodyVariant` captures per-field content type and headers. v1 supports it but flags it as "advanced" in `inspect`.
- **Cookie parameters.** Modeled but rarely used; dispatcher sets them via `httpx`'s cookies arg.
- **Header parameters with reserved names** (`Content-Type`, `Authorization`, `Accept`). Stripped from the LLM-facing schema and set by the dispatcher/auth layer. Logged at `--verbose` if the spec defined them.
- **Deprecated operations.** Included by default. Config flag `include_deprecated: false` excludes them. Deprecated operations get a `[DEPRECATED]` prefix in their MCP description.
- **`additionalProperties` defaults.** OpenAPI's implicit default is `true`; we preserve it. We do **not** force `additionalProperties: false` on tool input schemas — LLMs sometimes pass extras and we shouldn't break them by default. Config can flip this.
- **Recursive schemas.** Kept as cycles via `$ref` to a `$defs` entry; Simplify decides whether to break the cycle for the LLM-facing view.

**Round-trip invariant (must hold for all stages):**

> For any Operation `op` and any valid set of arguments `args` accepted by `Expose(Simplify(op)).inputSchema`, the Dispatcher must be able to construct a complete, well-formed HTTP request to the upstream API using only `op` (the Operation Model) and `args`. Simplify must never strip information the Dispatcher needs.

In practice this means Simplify operates on a *projection* of the schema for LLM consumption, but the Operation Model retains the full schema for dispatch. Both are passed forward.

### 4.4 Simplify (the hard part)

This is the stage with the most product risk. From the requirements: "auto-generated tools are unusable by LLMs because schemas are too complex" is a High/High risk.

**Responsibilities:**
- Produce an **LLM-facing input schema** from each Operation's full schema, suitable for inclusion in an MCP tool definition.
- Apply a documented set of simplifications.
- Never strip information the Dispatcher needs (round-trip invariant from §4.3).

**Interface contract:**

```python
class SimplifiedOperation(BaseModel):
    operation: Operation              # unchanged, full fidelity for dispatch
    llm_input_schema: dict[str, Any]  # what the LLM sees as the tool input
    llm_description: str              # truncated, possibly synthesized
    arg_map: ArgumentMap              # how LLM args reconstruct into HTTP request parts
    warnings: list[SimplifyWarning]   # what was lost or flagged for user attention
```

The `ArgumentMap` is the explicit bridge from LLM-facing arguments to dispatch:

```python
class ArgumentMap(BaseModel):
    # For each top-level key in llm_input_schema, where does it go?
    # Resolves collisions from flattening (a path param `id` and a body field `id` get distinct llm names).
    bindings: dict[str, ArgumentBinding]

class ArgumentBinding(BaseModel):
    source_llm_key: str               # what the LLM sees
    target_kind: Literal["path", "query", "header", "cookie", "body_field", "body_root"]
    target_path: list[str]            # JSON pointer-like path into the dispatch destination
    style: ParamStyle | None = None   # carried from Parameter for serialization
    explode: bool | None = None
```

This makes the LLM-facing simplification explicit, traceable, and testable. Every binding can be unit-tested by feeding an example argument and asserting the resulting HTTP request part.

**The simplifications, in order:**

1. **Inline shallow refs.** A ref used only once and pointing to a small object gets inlined. Threshold: ref body under 20 properties and one referrer. Deeply shared refs stay as `$defs`.
2. **Drop spec-only metadata.** `example`, `xml`, `externalDocs`, `discriminator`, vendor extensions (`x-*`) are stripped from the LLM-facing schema. Preserved in the Operation Model.
3. **Collapse `oneOf` / `anyOf` where possible.** If all branches share the same primitive type, collapse to that type with an enum or pattern. If branches are structurally similar objects, merge with optional fields. Otherwise, keep as-is. **The ArgumentMap records which branch the LLM args came from** so the dispatcher reconstructs correctly.
4. **Flatten single-property wrappers.** `{ "data": {actual fields} }` patterns become their inner shape in the LLM schema; the ArgumentMap records the wrapping so the dispatcher re-wraps before sending. Input only — response shapes pass through.
5. **Truncate descriptions.** Cap field descriptions at 500 chars in the LLM schema; full text in the Operation Model.

**Decisions:**
- All five simplifications are on by default. Each is individually disableable via config.
- Schemas that can't be safely simplified (e.g. arbitrary recursion) fall back to a tool with a single `string` "free-form JSON" argument plus a clear description. Logged as a `SimplifyWarning`.
- Each simplification is a pure function over the schema. Pipeline composes them. The ArgumentMap is built up incrementally as each simplification runs.

**This stage gets its own design doc before implementation**, including: the full set of `SimplifyWarning` kinds, the corpus-validated defaults, and decision trees for non-obvious cases (deep `allOf` chains, `discriminator`-tagged unions, mixed enum + freeform strings).

### 4.5 Expose (Tool Registry)

**Responsibilities:**
- Turn Operations into MCP tools.
- Build the lookup map from tool name → Operation.
- Apply final overrides (per-operation rename/hide/redescribe).
- Produce the catalog returned by MCP `tools/list`.

**Decisions:**
- The registry is built once at startup and held immutably for the server's lifetime. Hot-reload (P1) replaces it atomically.
- Each tool's `inputSchema` is the simplified schema from §4.4. The `description` is `summary` + `description` from the spec, truncated, with the upstream HTTP method and path appended for the LLM's benefit (e.g. `[GET /users/{id}]`).

### 4.6 MCP Server Runtime

**Tech:** Official MCP Python SDK (`mcp` on PyPI), `asyncio`-based.

**Transports:**
- **stdio** (default) — primary integration path for MCP clients like Claude Desktop.
- **HTTP+SSE** — opt-in via `--transport http --port N`. Pinned to the MCP version we target (see §10 risks).

#### 4.6.1 Tool invocation pipeline

```
tools/call(name, args)
   │
   ▼
1. Lookup       — find SimplifiedOperation by tool name
2. Validate     — args against llm_input_schema (jsonschema)
3. Map          — args + ArgumentMap → DispatchRequest
4. Authenticate — AuthScheme.apply() mutates DispatchRequest
5. Send         — httpx.AsyncClient.send()
6. Receive      — handle response or exception
7. Map result   — HTTP response → MCP tool result
8. Log + return — emit invocation log; return content blocks
```

Stages 1–4 are pure / in-process; stage 5 is the only I/O boundary. Stages 6–7 are pure.

#### 4.6.2 Argument-to-request mapping

The Dispatcher walks the ArgumentMap (§4.4) once per call:

- **`path` binding** → format the path template using `arg_value` with the parameter's `style`/`explode` (defaults: `style=simple`, `explode=false`).
- **`query` binding** → serialize into the URL query. Supported styles: `form` (default), `spaceDelimited`, `pipeDelimited`, `deepObject`. `deepObject` (used heavily by Stripe) emits keys like `card[number]=...`.
- **`header` binding** → set request header. Reserved headers (`Content-Type`, `Authorization`, `Accept`) are never settable this way; they're owned by the auth layer and content negotiation.
- **`cookie` binding** → set on the httpx `cookies` arg.
- **`body_field` binding** → write into the request body at `target_path`. For JSON bodies, write into the dict structure. For `application/x-www-form-urlencoded`, accumulate into the form. For `multipart/form-data`, use the `encoding` metadata to decide field vs file.
- **`body_root` binding** → entire LLM arg becomes the body root (rare; happens when the body schema is a non-object).

**Name-collision handling.** When Simplify flattens nested structures, two original parameters may map to the same LLM-facing name. The ArgumentMap resolves this by suffixing (`id`, `id_body`, `id_query`), and the LLM-facing schema documents the disambiguation in the field description. The original names are preserved in the Operation Model.

**Content negotiation.**
- Request: dispatcher sets `Content-Type` from the chosen `RequestBodyVariant`.
- Request: dispatcher sets `Accept` to the union of declared response content types. If the spec declares JSON, JSON is preferred.
- Response: dispatcher picks the matching `ResponseVariant` by upstream `Content-Type`. Unknown content types are still returned to the LLM as text with a `[Warning: undeclared content type X]` prefix.

#### 4.6.3 Response → MCP tool result mapping

Result format rules, in order:

1. **Body is empty / no content (204, 205).** Return a text block: `"Request succeeded (HTTP <code>). No response body."`
2. **Body is JSON** (`application/json` or `+json` suffix). Pretty-print with 2-space indent, return as a single text block. JSON is preferred over raw passthrough so the LLM can read it.
3. **Body is textual non-JSON** (`text/*`, `application/xml`, `application/yaml`). Return as a single text block.
4. **Body is binary** (anything else, or `Content-Type` matches a known binary type list). Return as an MCP resource content block with a `data:` URI (base64-encoded) and the upstream `Content-Type`. **Size cap: 1 MiB.** Above the cap, return a text block summarizing the response (status, content type, size) and decline to include the body. No streaming in v1.
5. **Response size > 1 MiB for text/JSON.** Truncate to the first 256 KiB, append a clear notice: `[truncated: response was N bytes, showing first 262144]`. The size threshold and behavior are config-overridable.

**Streaming responses (SSE, chunked).** Not supported in v1. Detect and refuse with a clear error. Tracked for v2.

**Pagination.** v1 does **not** auto-paginate. The Operation Model surfaces pagination parameters to the LLM as ordinary parameters; the LLM is responsible for following pagination. Config can later mark an operation as `auto_paginate` (v2 feature).

#### 4.6.4 Error handling at the runtime boundary

The Dispatcher catches everything and maps to typed errors. See §4.10 for the full taxonomy. Summary:

- **Upstream 4xx** → `UpstreamClientError`. Body is included in the MCP error content (truncated to 4 KiB). The LLM sees enough to understand what went wrong.
- **Upstream 5xx** → `UpstreamServerError`. Sanitized message + a request-id correlation token. The full body goes to logs only.
- **Network errors** (DNS, connect, TLS, read timeout) → `TransientError`. The MCP error includes `transient: true` so MCP clients / orchestrators can decide whether to retry at their layer.
- **Validation errors** (LLM-supplied args fail the input schema) → `ArgumentValidationError`. Includes the JSON Schema error path. Returned before any HTTP call is made.
- **Mapping errors** (ArgumentMap produces an invalid HTTP request, e.g. unfillable path template) → `DispatchError`. Indicates a bug in specmcp; logged at ERROR with full context.

**No automatic retry inside a tool call.** Period. LLM tool calls have side effects we can't know about. Retries are an explicit per-operation config flag (`retry: { attempts: N, on_status: [503] }`) that the user opts into. Even with retries enabled, we only retry on idempotent methods (GET, HEAD, PUT, DELETE) and explicitly listed 5xx codes.

#### 4.6.5 Concurrency model

- Global cap on concurrent in-flight tool calls: 32 by default (configurable). Implemented as a top-level `asyncio.Semaphore`. Calls exceeding the cap queue.
- Per-upstream-host cap: 10 by default (configurable). Implemented as one semaphore per host, keyed by `server_url` netloc.
- Per-call timeout: 30s by default. Per-operation override via config. Hard cap at 5 minutes.
- **Cancellation.** If the MCP client cancels the call (transport allowing), `asyncio.CancelledError` propagates; the httpx request is aborted via context-manager teardown. We log the cancellation and do not return a result.
- **Shutdown.** On `SIGINT`/`SIGTERM`, the server stops accepting new `tools/call` requests immediately but allows in-flight calls a grace period (default 10s) to finish. After grace period, in-flight tasks are cancelled. The server then closes all `httpx.AsyncClient`s and exits 0.

#### 4.6.6 Logging schema

Each tool invocation emits one structured log line at INFO via `structlog`:

```json
{
  "event": "tool_invocation",
  "tool_name": "get_user_by_id",
  "operation_id": "getUserById",
  "method": "GET",
  "upstream_host": "api.example.com",
  "upstream_status": 200,
  "duration_ms": 142,
  "request_id": "01J...",
  "outcome": "success",
  "args_keys": ["id"],
  "response_size_bytes": 384,
  "auth_scheme": "petstoreApiKey"
}
```

Redaction rules:
- Argument *values* are never logged at INFO; only the keys. `--verbose` (DEBUG) logs values with auth-config-marked fields redacted as `<redacted:scheme_name>`.
- Response bodies are never logged at INFO. At DEBUG, the first 1 KiB is logged with the same redaction.
- Auth header values are never logged at any level.

Errors emit a second log line at ERROR with the error taxonomy fields (§4.10).

### 4.7 Auth Layer

**v1 scope:** API key (header/query/cookie) and Bearer tokens.

**Design:**
- Auth schemes declared in the OpenAPI `securitySchemes`, selected per-operation via `security`.
- Auth values come from env vars referenced in `mcp.config.yaml`:
  ```yaml
  auth:
    petstoreApiKey:
      type: apiKey
      in: header
      name: X-API-Key
      valueFrom: env(PETSTORE_API_KEY)
  ```
- Missing env vars at startup → loud error from `validate` / `serve`. Never start partially-authed.
- Logs redact all auth values, identified by the auth config rather than pattern matching.
- Implementation: an `AuthScheme` protocol with two methods to accommodate v1.1+ schemes that need response visibility (OAuth token refresh):

  ```python
  class AuthScheme(Protocol):
      async def apply(self, request: httpx.Request) -> httpx.Request:
          """Mutate the request to include credentials."""

      async def handle_response(
          self, request: httpx.Request, response: httpx.Response
      ) -> Literal["accept", "retry"]:
          """Inspect the response. Return 'retry' to re-attempt with refreshed creds."""
  ```

  v1 concrete classes (`ApiKeyAuth`, `BearerAuth`) always return `"accept"` from `handle_response`. v2's `OAuth2Auth` will use `"retry"` on 401 to trigger token refresh, with a hard limit of one retry per call. The dispatcher loop honors this contract regardless of scheme.

**Not in v1:** OAuth, Basic, mTLS, custom signing schemes. The `AuthScheme` protocol means v1.1+ additions slot in without touching the dispatcher.

### 4.8 HTTP Client

**Tech:** `httpx` with `AsyncClient`.

**Responsibilities:**
- Execute the HTTP request built by the dispatcher.
- Apply timeouts (default 30s, config-overridable per-operation).
- Apply concurrency limits per upstream host (default 10, configurable) via `asyncio.Semaphore`.
- Surface upstream rate-limit responses to the LLM as a typed error.

**Decisions:**
- No caching in v1. Caching for LLM tool calls is product-sensitive (staleness vs latency) and deferred until we understand usage patterns.
- No automatic retry. (See §4.6.)
- TLS verification on by default. `--insecure` flag exists for local dev only and emits a startup warning.
- One `AsyncClient` per upstream host, reused for connection pooling.

### 4.9 Telemetry

**Tech:** Custom thin wrapper over `httpx.AsyncClient.post` to a single endpoint. No third-party SDK in v1 to keep the dependency surface small and the privacy story simple.

**Implementation:**
- Disabled by default. Enabled via `SPECMCP_TELEMETRY=1` or `--telemetry`.
- On enable, prints a one-time notice describing what is sent.
- Payload schema is versioned and small. See requirements §9 for exact contents.
- Failures to send are silent and never block CLI operation. Fire-and-forget via `asyncio.create_task` with exception logging only at `--verbose`.
- A `--telemetry-dry-run` flag prints what would have been sent without sending.

### 4.10 Error Taxonomy

All errors inherit from `SpecmcpError`. The taxonomy is fixed; new error kinds require a design-doc update.

```
SpecmcpError                               # base
├── ConfigError                            # bad config, missing env vars, unresolved refs in config
├── SpecError                              # any problem with the input spec
│   ├── SpecSyntaxError                    # YAML/JSON parse failure
│   ├── SpecValidationError                # fails OpenAPI meta-schema
│   ├── SpecResolutionError                # $ref can't be resolved
│   └── SpecUnsupportedError               # valid spec, feature not yet supported (e.g. Swagger 2)
├── PipelineError                          # internal pipeline failures
│   ├── NormalizeError
│   └── SimplifyError
├── RuntimeError                           # raised during tools/call
│   ├── ArgumentValidationError            # LLM args fail input schema
│   ├── DispatchError                      # ArgumentMap → HTTP construction failed (specmcp bug)
│   ├── AuthError                          # auth scheme couldn't apply or response refused
│   ├── UpstreamClientError                # 4xx from upstream
│   ├── UpstreamServerError                # 5xx from upstream
│   ├── TransientError                     # network, DNS, TLS, timeout
│   └── ResponseTooLargeError              # response exceeded configured cap
└── InternalError                          # genuine specmcp bugs; never user-facing without context
```

**Each error class carries the same shape:**

```python
class SpecmcpError(Exception):
    code: str                  # stable identifier like "spec.resolution_failed"
    message: str               # human-readable, one line
    detail: str | None = None  # multi-line, optional
    location: SourceLocation | None = None  # for spec errors
    request_id: str | None = None           # for runtime errors
    context: dict[str, Any] = {}            # structured context
```

**Mapping to MCP error responses (runtime only):**

| Error class | MCP `isError` | Content blocks shown to LLM |
|---|---|---|
| `ArgumentValidationError` | true | Schema error path + which arg failed |
| `UpstreamClientError` (4xx) | true | Status + truncated body + interpretation hint |
| `UpstreamServerError` (5xx) | true | "Upstream service error" + request_id |
| `TransientError` | true | "Network error" + request_id; metadata `transient: true` |
| `AuthError` | true | "Authentication failed" + request_id; **no detail to LLM** |
| `ResponseTooLargeError` | true | "Response too large (N bytes)" + size cap |
| `DispatchError` / `InternalError` | true | "Internal error" + request_id; **full detail logged only** |

**Mapping to CLI exit codes:**

| Error class | Exit code | Stream |
|---|---|---|
| (success) | 0 | — |
| `ConfigError` | 64 | stderr |
| `SpecSyntaxError` / `SpecValidationError` / `SpecResolutionError` | 65 | stderr |
| `SpecUnsupportedError` | 69 | stderr |
| `PipelineError` | 70 | stderr |
| `InternalError` | 70 | stderr |

Exit codes follow `sysexits.h` conventions. Machine-readable error output via `--json` always serializes the full `SpecmcpError` shape; human-readable output gets the formatted version with location context.

### 4.11 Config Schema

The config file is `mcp.config.yaml` by default; path overridable via `--config`. Validated at load time with Pydantic.

```yaml
# specmcp v1 config schema (version: "1")
version: "1"

spec:
  source: "https://api.example.com/openapi.json"   # path or URL; required
  cache: true                                       # cache resolved spec by content hash

server:
  base_url_override: null                           # optional; replaces servers[0]
  include_deprecated: true
  include_tags: []                                  # if non-empty, only operations with these tags
  exclude_tags: []
  include_operations: []                            # if non-empty, allowlist by operation id
  exclude_operations: []

auth:
  # keyed by name matching securitySchemes in the spec
  petstoreApiKey:
    type: apiKey                                    # apiKey | bearer
    in: header                                      # for apiKey: header | query | cookie
    name: X-API-Key
    value_from: env(PETSTORE_API_KEY)

dispatch:
  default_timeout_seconds: 30
  per_host_concurrency: 10
  global_concurrency: 32
  response_size_limit_bytes: 1048576                # 1 MiB
  text_truncate_bytes: 262144                       # 256 KiB
  tls_verify: true

simplify:
  inline_shallow_refs: true
  drop_spec_metadata: true
  collapse_unions: true
  flatten_single_property_wrappers: true
  truncate_description_chars: 500

operations:
  # per-operation overrides, keyed by operation id (post-naming)
  getUserById:
    rename: get_user                                # override the tool name
    description: "Fetch a user by ID. Pass `id` as a string."
    hide: false
    server_url: "https://staging-api.example.com"   # override server for this op
    timeout_seconds: 60                             # override dispatch timeout
    retry:
      attempts: 2
      on_status: [503, 504]                         # only idempotent methods
    pin_request_body_variant: "application/json"
    additional_properties_strict: false             # force additionalProperties:false in input

transport:
  default: stdio                                    # stdio | http
  http:
    host: 127.0.0.1
    port: 8765

telemetry:
  enabled: false

logging:
  level: info                                       # debug | info | warn | error
  format: json                                      # json | text
```

**Conflict resolution between CLI flags and config:** CLI wins. Config values are defaults. Documented precedence in `--help`.

**Schema versioning.** The top-level `version` key is required. Future breaking changes bump it; the loader rejects unknown versions with a clear error.

### 4.12 Command Behaviors

#### `specmcp init <spec>`

Algorithm:

1. Load and validate the spec.
2. Detect declared `securitySchemes`. For each:
   - If supported in v1 (apiKey, bearer): generate an entry in `auth:` with `value_from: env(<UPPERCASED_SCHEME_NAME>)` and add the env var to a generated `.env.example`.
   - If not supported: include a commented-out entry with `# TODO: not supported in v1` and a doc link.
3. Generate a config skeleton with all sections present, defaults filled in, and the top-level `version: "1"`.
4. Write `mcp.config.yaml` (refuse to overwrite without `--force`).
5. Write `.env.example` listing every required env var.
6. Print a summary: number of operations, number of auth schemes detected, next steps.

`init` does not enumerate operations in the config (84 entries would be unmanageable). Per-operation overrides are added by hand by the user.

`--interactive` flag (P1) prompts for the spec URL, picks a transport, and offers auth defaults.

#### `specmcp validate`

Runs Load + Normalize. Reports:
- Spec syntax/validation errors (with file/line).
- Unresolved refs.
- Config errors (missing env vars, version mismatch, schema invalid).
- Operations that will fall back to "free-form JSON" in Simplify (warning, not error).

Exit codes per §4.10. `--json` emits a structured report.

#### `specmcp inspect`

Runs the full pipeline. Outputs (human or `--json`):

- Summary line: `N operations, M tools, K fallbacks, J warnings`.
- For each tool: name, upstream method+path, description, `inputSchema` (pretty), notable Simplify warnings, the auth scheme(s) it requires.
- Naming algorithm conflicts (collisions resolved by suffix) listed at top.

`--json` schema is stable and documented; consumers can pipe it into CI.

#### `specmcp serve`

Runs the full pipeline, starts the runtime. SIGINT/SIGTERM handling per §4.6.5.

#### `specmcp report-issue`

Bundles a sanitized debug report:

1. The user's config with all `value_from: env(...)` resolved values **omitted entirely** (not redacted-as-string; the key is replaced with `"<env-var: NAME>"`).
2. A spec excerpt limited to the operations involved in the failure (if the user passed `--operation`), or the full spec stripped of `examples` and `x-*` extensions.
3. specmcp version, Python version, OS, MCP SDK version.
4. The last N log lines (default 200) with argument values redacted per §4.6.6.
5. Any captured exception with its full taxonomy.

Output is a single `specmcp-report-<timestamp>.tar.gz`. The user is shown a summary of what's included and asked to confirm before the file is written.

### 4.13 MCP Version Policy

Listed as a High/High risk in requirements §10. The policy:

- specmcp pins to a specific MCP spec version per release. The pinned version is in `pyproject.toml` (via the `mcp` SDK constraint) and surfaced in `specmcp --version`.
- The pinned version is exercised in CI against the MCP conformance tests for `serve`.
- On an MCP spec release: file an issue, run the conformance suite against the new version, decide whether to upgrade. If we upgrade, it's a minor version bump for specmcp; if the MCP change is breaking for users' clients, it's a major bump.
- Users can see the supported MCP version in `specmcp --version` and in the README compatibility table.
- We do not support multiple MCP versions in one specmcp release. Pin one, document it.

## 5. Worked Example: Petstore End-to-End

This section walks the Petstore spec through every stage. It is the canonical reference for "what does each stage actually produce" and acts as an integration test scaffold.

### 5.1 Input spec (excerpt)

```yaml
openapi: 3.0.3
info: { title: Petstore, version: 1.0.0 }
servers:
  - url: https://petstore.example.com/v1
paths:
  /pets/{petId}:
    get:
      operationId: getPetById
      summary: Get a pet by ID
      parameters:
        - name: petId
          in: path
          required: true
          schema: { type: integer, format: int64 }
        - name: verbose
          in: query
          schema: { type: boolean, default: false }
      responses:
        '200':
          description: A pet
          content:
            application/json:
              schema: { $ref: '#/components/schemas/Pet' }
        '404':
          description: Not found
      security:
        - petstoreApiKey: []
components:
  schemas:
    Pet:
      type: object
      required: [id, name]
      properties:
        id: { type: integer, format: int64 }
        name: { type: string }
        tag: { type: string, nullable: true }
  securitySchemes:
    petstoreApiKey:
      type: apiKey
      in: header
      name: X-API-Key
```

### 5.2 After Load + Parse

`prance` produces the resolved spec with `$ref` to `Pet` inlined. ruamel preserves line numbers for error reporting (the `Pet` schema is recorded as being defined at `petstore.yaml:24`).

### 5.3 After Normalize

One `Operation` is produced:

```python
Operation(
    id="getPetById",
    method="GET",
    path="/pets/{petId}",
    server_url="https://petstore.example.com/v1",
    parameters=[
        Parameter(
            name="petId", location="path", required=True,
            schema_={"type": "integer", "format": "int64"},
            style="simple", explode=False,
        ),
        Parameter(
            name="verbose", location="query", required=False,
            schema_={"type": "boolean", "default": False},
            style="form", explode=True,
        ),
    ],
    request_body=None,
    responses=[
        Response(
            status_code="200",
            description="A pet",
            variants=[ResponseVariant(
                content_type="application/json",
                schema_={
                    "type": "object",
                    "required": ["id", "name"],
                    "properties": {
                        "id":   {"type": "integer", "format": "int64"},
                        "name": {"type": "string"},
                        "tag":  {"type": ["string", "null"]},  # normalized from 3.0 nullable
                    },
                },
            )],
        ),
        Response(status_code="404", description="Not found", variants=[]),
    ],
    auth=[[AuthRequirement(scheme_name="petstoreApiKey", scopes=[])]],
    summary="Get a pet by ID",
    description=None,
    tags=[],
    deprecated=False,
    source_location=("petstore.yaml", 8),
)
```

Note `nullable: true` on `tag` has been normalized to `type: ["string", "null"]` (3.1 form).

### 5.4 After Simplify

```python
SimplifiedOperation(
    operation=<above>,
    llm_input_schema={
        "type": "object",
        "required": ["petId"],
        "properties": {
            "petId":   {"type": "integer", "description": "..."},
            "verbose": {"type": "boolean", "default": False},
        },
        "additionalProperties": True,
    },
    llm_description="Get a pet by ID [GET /pets/{petId}]",
    arg_map=ArgumentMap(bindings={
        "petId":   ArgumentBinding(
            source_llm_key="petId", target_kind="path",
            target_path=["petId"], style="simple", explode=False,
        ),
        "verbose": ArgumentBinding(
            source_llm_key="verbose", target_kind="query",
            target_path=["verbose"], style="form", explode=True,
        ),
    }),
    warnings=[],
)
```

No fallback needed; this is a clean case.

### 5.5 After Expose

The tool registry contains one entry: `getPetById` → SimplifiedOperation above. The MCP `tools/list` response (truncated) includes:

```json
{
  "name": "getPetById",
  "description": "Get a pet by ID [GET /pets/{petId}]",
  "inputSchema": {
    "type": "object",
    "required": ["petId"],
    "properties": {
      "petId":   {"type": "integer"},
      "verbose": {"type": "boolean", "default": false}
    }
  }
}
```

### 5.6 Tool call: success path

MCP client sends:

```json
{ "method": "tools/call",
  "params": { "name": "getPetById", "arguments": { "petId": 42 } } }
```

Dispatch:

1. **Lookup**: SimplifiedOperation found.
2. **Validate**: `{"petId": 42}` against `llm_input_schema` → passes.
3. **Map**:
   - ArgumentMap walks: `petId` → path binding with `style=simple` → path becomes `/pets/42`.
   - `verbose` not supplied; default `false` from the schema is **not** sent (we only send provided args).
   - DispatchRequest constructed:
     ```
     GET https://petstore.example.com/v1/pets/42
     Accept: application/json
     ```
4. **Authenticate**: `ApiKeyAuth.apply()` reads `PETSTORE_API_KEY` env var, sets `X-API-Key: sk_test_...`.
5. **Send**: httpx returns 200 with body `{"id": 42, "name": "Fluffy", "tag": null}`.
6. **Receive**: status 200, content type `application/json`, size 38 bytes.
7. **Map result**: JSON → pretty-printed text block.
8. **Log**:
   ```json
   {"event": "tool_invocation", "tool_name": "getPetById",
    "method": "GET", "upstream_host": "petstore.example.com",
    "upstream_status": 200, "duration_ms": 87,
    "args_keys": ["petId"], "outcome": "success", "auth_scheme": "petstoreApiKey"}
   ```
9. **Return** to MCP client:
   ```json
   { "content": [{"type": "text",
                  "text": "{\n  \"id\": 42,\n  \"name\": \"Fluffy\",\n  \"tag\": null\n}"}],
     "isError": false }
   ```

### 5.7 Tool call: failure paths

**404 from upstream.** Dispatcher returns `UpstreamClientError`. MCP response:

```json
{ "content": [{"type": "text", "text": "Upstream returned HTTP 404: Not found"}],
  "isError": true,
  "_meta": {"specmcp": {"code": "upstream.client_error", "status": 404, "request_id": "01J..."}} }
```

**Bad argument.** LLM sends `{"petId": "not-a-number"}`. `ArgumentValidationError` returned before any HTTP call:

```json
{ "content": [{"type": "text", "text": "Invalid argument: petId must be an integer (got string)"}],
  "isError": true,
  "_meta": {"specmcp": {"code": "argument.validation_failed", "path": "/petId"}} }
```

**Network error.** Upstream times out. `TransientError`. MCP response includes `transient: true` in `_meta` so an MCP-aware orchestrator can decide whether to retry.

### 5.8 What this example demonstrates

- The Operation Model captures everything the Dispatcher needs (the round-trip invariant holds).
- The ArgumentMap is the single source of truth for how LLM-facing args become HTTP request parts. No magic in the dispatcher.
- `style`/`explode` from OpenAPI flow through to httpx serialization.
- The error taxonomy maps cleanly to MCP responses.

Petstore is the easy case. A second worked example (Stripe's `deepObject` query params plus OAuth deferred to v2) lives in the Simplify design doc — that's the right place because it exercises Simplify branches Petstore doesn't.

## 6. Test Strategy

**Unit tests:** `pytest` + `pytest-asyncio`. Each pipeline stage has hand-built fixtures.

**Integration tests:** a corpus of 15 real-world OpenAPI specs in CI. Test corpus includes:

- Small/clean: Petstore, JSONPlaceholder
- Medium SaaS: Stripe (~450 ops), Linear (~120 ops), GitHub (~900 ops)
- Spec edge cases: AWS API Gateway (huge), DigitalOcean (heavy polymorphism), Slack (mixed auth)
- Adversarial: deliberately broken specs we constructed to exercise error paths

For each spec we measure:
- Conversion success rate (tools generated / operations in spec).
- Number of fallback tools (the "free-form JSON" path).
- Startup time.
- Tool inspection output snapshot (for regression detection).

The **conversion success rate** is reported in CI and gates releases. Requirements §12 sets the bar at 80% across this corpus.

**Snapshot testing:** `syrupy` for `inspect` output regression.

**LLM-usability tests:** for the top 3 corpus APIs, we run a small fixed eval where Claude is given the generated tools and a set of tasks. Tracked as a release-quality signal, not a gating CI test (cost and flakiness reasons).

**Conformance:** the MCP SDK's protocol tests plus the MCP Inspector tool, run against `serve` in CI.

**Test corpus licensing.** We do not vendor copyrighted specs into the repo. The corpus is a manifest (`test-corpus/manifest.yaml`) listing URLs, expected SHA256, and a per-spec license note. CI fetches them at run time and caches in a CI artifact. Permissive-licensed specs (Petstore, JSONPlaceholder, things explicitly under MIT/CC-BY) we vendor for speed. AWS, Stripe, GitHub, etc. are fetched, never vendored. The fetch step is a separate CI job whose result feeds the integration job.

## 7. Distribution and Packaging

- Single PyPI package `specmcp`. Install via `pip install specmcp` or `pipx install specmcp` (pipx recommended in the README to avoid env pollution).
- Standalone binaries for macOS (arm64, x64) and Linux (x64, arm64) via `shiv` (zipapp-based, fast startup, no native packaging complexity). Distributed via GitHub Releases.
- Considered and rejected: `pyinstaller` (slower startup, larger binaries, occasional false positives from antivirus). `shiv` keeps binaries small and Python-shaped, but requires Python on the target machine. We document this in the install guide and provide `pyinstaller`-built fully-standalone binaries as a secondary distribution if user feedback demands it.
- Build via `uv` or `hatch`; release process via `python -m build` + `twine`, automated through GitHub Actions.
- Semver, automated changelog from conventional commits, signed GitHub releases.

## 8. Repository Layout

```
specmcp/
├── src/
│   └── specmcp/
│       ├── cli/              # typer entry, command modules
│       ├── core/             # pipeline: load, normalize, simplify, expose
│       ├── runtime/          # MCP server, dispatcher, http client
│       ├── auth/             # auth schemes (extensible via Protocol)
│       └── telemetry/        # opt-in telemetry client
├── tests/
│   ├── unit/
│   └── integration/
├── test-corpus/              # 15 real-world specs, pinned versions
├── examples/                 # 3 runnable examples (req §6)
├── docs/                     # user docs, naming algorithm, simplify rules
├── design/                   # this doc + future design docs
├── pyproject.toml
└── README.md
```

Single package, src-layout. `uv` for dependency and venv management; `pyproject.toml` is the single source of truth.

## 9. Performance Budget

From requirements §7.1:

- Startup for ~450-op spec: under 5s.
- Per-call overhead: under 50ms p95 excluding upstream.

Breakdown estimate at startup for a 450-op spec (Python adds ~300-500ms interpreter startup vs Node):

| Stage | Budget |
|---|---|
| Python interpreter + imports | 400ms |
| Spec fetch (local) | 50ms |
| Parse + ref resolution (prance) | 1500ms |
| Normalize | 500ms |
| Simplify | 2000ms |
| Expose + server boot | 500ms |
| **Total** | **~4.95s** |

We are close to the 5s budget. Mitigations if we exceed it:

- Lazy imports in CLI entry to avoid loading unused modules.
- Parallelize Simplify across operations (it's embarrassingly parallel; either `asyncio` or `concurrent.futures.ProcessPoolExecutor` for CPU-bound work).
- Cache the resolved spec to disk keyed by content hash, so repeat starts skip prance.

Per-call overhead breakdown:

| Stage | Budget |
|---|---|
| Argument validation (jsonschema) | 8ms |
| Request construction | 5ms |
| Auth application | 2ms |
| httpx overhead | 12ms |
| Response mapping | 10ms |
| Logging + telemetry | 5ms |
| **Total** | **~42ms** |

`jsonschema` is the largest line; if it becomes a bottleneck we switch to `fastjsonschema` (precompiles schemas, ~5-10x faster) at the cost of slightly weirder error messages.

## 10. Open Implementation Questions

These are decisions deferred until implementation starts, listed so they're not lost:

- **Build tool:** `uv` (newer, very fast, opinionated) or `hatch` (more conventional, more extensible). Lean: `uv` for speed unless we hit a feature gap.
- **JSON Schema validator:** `jsonschema` (canonical, good errors) vs `fastjsonschema` (fast, worse errors). Lean: start with `jsonschema`; profile and switch if needed.
- **HTTP+SSE transport details:** the MCP spec is still evolving here. Pin to the spec version we target and revisit if it changes.
- **`init` UX:** interactive prompts (questionary-style) or pure file-scaffold? Lean: pure scaffold + `--interactive` flag.
- **Logging library:** `structlog` (JSON-first, async-friendly, great context handling) is the default unless we hit a reason otherwise.
- **Config schema validation:** Pydantic for `mcp.config.yaml` (we're already using Pydantic for the Operation Model — keep it consistent).

## 11. What This Design Defers

Explicitly out of scope for v1, called out so they're not accidentally built:

- Codegen mode (v1.1).
- GraphQL/Postman ingestion (v2).
- OAuth/Basic auth (v2/v1.1).
- Plugin interface (v2).
- Hot-reload (P1).
- Caching layer.
- Multi-spec / multi-server merging.
- Spec diffing.

## 12. Risks Specific to This Design

| Risk | Mitigation |
|---|---|
| Simplify stage produces schemas LLMs still can't use | Dedicated design doc; LLM-usability eval before GA; conservative defaults; clear escape hatch (free-form JSON fallback). |
| `prance` hits a real-world spec it can't resolve | Test corpus catches this in CI; we wrap prance behind our own interface so we can swap to `jsonref` or a custom resolver if needed. |
| Python startup time pushes us over the 5s budget | Lazy imports, content-hash cache for resolved specs, parallel Simplify. Measure on Stripe spec at first prototype milestone. |
| Per-call overhead grows past 50ms with logging + validation | Profiling gate in CI on every release; budget tracked in §9; `fastjsonschema` as escape hatch. |
| Single-binary distribution via shiv requires Python on target | Document clearly; offer pyinstaller-built fallback if feedback demands it. |
| MCP Python SDK churn breaks our integration | Pin SDK version; CI runs against the pinned version; upgrade is an explicit task with its own PR. |

---

## Appendix: Decision Log

| Decision | Choice | Why |
|---|---|---|
| Implementation language | Python 3.11+ | Team preference and familiarity; MCP Python SDK is mature; async stack is solid for I/O-bound proxy. |
| OpenAPI parser | `prance` + `openapi-spec-validator` | Best-maintained Python option for ref resolution + meta-schema validation. |
| Internal data model | Pydantic v2 | Validation at stage boundaries; clean error messages; consistent with config validation. |
| HTTP client | `httpx` (async) | Async-native, modern, well-maintained; reuses connection pool per host. |
| CLI framework | `typer` | Type-hint-driven, low ceremony, built on Click. |
| Test framework | `pytest` + `pytest-asyncio` + `syrupy` | Standard Python stack; snapshots for `inspect` output. |
| Internal schema dialect | JSON Schema 2020-12 (OpenAPI 3.1-aligned) | Forward-looking; matches MCP tool input format. |
| Default transport | stdio | Primary MCP integration path; matches Claude Desktop and others. |
| Retry policy | None by default | LLM tool calls with side effects shouldn't silently duplicate. |
| Caching | None in v1 | Staleness vs latency is product-sensitive; defer until we see usage. |
| Simplify defaults | All five on | Validated against corpus; users can disable per-rule. |
| Telemetry default | Off | Trust > data. |
| Package manager | `uv` (provisional) | Fast, modern; revisit if it lacks a feature we need. |
| Binary distribution | `shiv` | Smaller and faster than pyinstaller; pyinstaller as fallback if needed. |
| Operation Model — single representation | Yes | All stages share one model; no second internal IR. Edges (3.0 vs 3.1) normalized at Normalize. |
| LLM-facing schema vs dispatch schema | Separate | Simplify produces a projection; Operation Model keeps full fidelity. Round-trip invariant enforced. |
| ArgumentMap as explicit bridge | Yes | Makes LLM-arg → HTTP mapping testable and auditable; no implicit magic in the dispatcher. |
| Auth Protocol shape | `apply` + `handle_response` | v1 schemes return `accept`; v2 OAuth can return `retry`. Keeps dispatcher loop simple. |
| Response size cap | 1 MiB binary, 256 KiB text truncate | Prevents catastrophic LLM context overflow; configurable. |
| Streaming/pagination | Not in v1 | Streaming refused with clear error. Pagination params exposed; LLM handles. |
| Auto-retry on transient errors | Off by default | LLM tool calls can have side effects; retry only on explicit opt-in + idempotent methods. |
| Test corpus distribution | Manifest + fetch | Avoids vendoring copyrighted specs; permissive ones vendored for speed. |
| Error taxonomy | Fixed hierarchy in §4.10 | Stable codes for CLI exit and MCP `_meta`; new kinds require design-doc update. |
| Config schema versioning | Top-level `version` key | Future breaking changes bump it; loader rejects unknown versions. |
| CLI vs config conflict | CLI wins | Config is defaults; flags override. Documented in `--help`. |
