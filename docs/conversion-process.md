# specmcp — OpenAPI to MCP Conversion Process

This document describes how specmcp converts an OpenAPI 3.x specification into a running MCP server, stage by stage. For a visual overview, see [`pipeline-diagram.svg`](./pipeline-diagram.svg).

---

## Overview

specmcp is a **proxy**, not a code generator. It reads an OpenAPI spec at startup, builds an in-memory registry of MCP tools, and forwards every `tools/call` request to the real upstream API — translating arguments and responses on the fly. No code is ever written to disk; the spec is the source of truth.

The conversion runs as a linear pipeline with five stages:

```
Load → Normalize → Simplify → Expose → MCP Server
```

Each stage is responsible for one concern and hands a well-typed data structure to the next.

---

## Stage 1 — Load (`src/specmcp/core/load.py`)

**Input:** a file path or URL pointing to an OpenAPI 3.0 or 3.1 spec (JSON or YAML).

**Output:** `ResolvedSpec` — a plain Python dict of the fully-dereferenced spec, plus metadata (`source`, `openapi_version`).

**What it does:**

- Detects whether the source is a local file or a remote URL and reads it accordingly.
- Uses `prance` to resolve all `$ref` pointers, including cross-file and HTTP references. After this step, every schema and parameter object is fully inlined — no `$ref` remains in the output.
- Validates the dereferenced spec against the OpenAPI 3.x JSON Schema using `openapi-spec-validator`. Validation errors are surfaced as `LoadError` with the source location.
- Detects the OpenAPI version from the `openapi` field (`3.0.*` vs `3.1.*`) and records it for downstream normalization.

The output is intentionally a raw dict rather than a typed model, because the spec surface area is too large to model completely and prance's dereferenced output is already a plain Python structure.

---

## Stage 2 — Normalize (`src/specmcp/core/normalize.py`)

**Input:** `ResolvedSpec`

**Output:** `list[Operation]` — one `Operation` per REST operation in the spec, in spec order.

**What it does:**

### 3.0 → 3.1 schema normalization

OpenAPI 3.0 and 3.1 use different JSON Schema dialects. Normalize bridges the gap so downstream stages only need to handle one form:

- `nullable: true` → adds `"null"` to the `type` field (or wraps a scalar type in a list).
- `exclusiveMaximum: true` (boolean, 3.0) → `exclusiveMaximum: <numeric value>` (3.1 form).
- `exclusiveMinimum: true` (boolean, 3.0) → `exclusiveMinimum: <numeric value>` (3.1 form).

These rules are applied recursively to every schema in the spec.

### Parameter merging

OpenAPI allows parameters at the path level and the operation level. Operation-level parameters override path-level ones with the same `(name, in)` pair. Normalize merges them and strips reserved headers (`Content-Type`, `Authorization`, `Accept`, `Content-Length`, `Host`, `Transfer-Encoding`) — these are owned by the auth and transport layers and must not be exposed as LLM-facing arguments.

### Operation ID derivation

Every operation needs a stable, unique ID used as the MCP tool name. If `operationId` is present in the spec, it is used directly. If absent, an ID is derived from the HTTP method and path:

```
GET /users/{id}  →  get_users_id
POST /orgs/{org}/repos  →  post_orgs_org_repos
```

Steps: replace `/` with `_`, strip `{` and `}`, collapse consecutive underscores, strip leading/trailing underscores.

Collisions (two operations producing the same derived ID) are resolved by appending `_2`, `_3`, etc. to duplicates in spec order. The first occurrence always keeps the undecorated name.

This algorithm is documented in [`naming.md`](./naming.md) and is **stable for v1** — any change is a breaking change.

### Server URL resolution

`servers[0]` from the spec is selected and its template variables resolved using their defaults (or overrides supplied via config). If any `{variable}` remains unresolved, a `NormalizeError` is raised at startup rather than silently producing a broken URL at request time.

### Filtering

Config-level filters are applied here:

- `include_deprecated: false` — drops operations with `deprecated: true`.
- `include_tags` / `exclude_tags` — filter by tag membership.
- `include_operations` / `exclude_operations` — filter by operation ID.

Filtered operations are dropped entirely and never reach the Simplify stage.

---

## Stage 3 — Simplify (`src/specmcp/core/simplify.py`)

**Input:** `list[Operation]`

**Output:** `list[SimplifiedOperation]` — each wrapping an `Operation` with an `ArgumentMap`, an LLM-facing description, and an LLM-facing JSON Schema (`llm_input_schema`).

**What it does:**

The Simplify stage bridges OpenAPI's request model (path, query, header, body) and the MCP tool model (a single flat JSON object of arguments). It does this by building an **ArgumentMap** — an explicit mapping from every LLM argument key to an HTTP binding target.

### Argument map construction

For each operation, Simplify walks the parameters and request body and assigns each a binding:

| Source | `target_kind` | `target_path` |
|---|---|---|
| Path parameter `{id}` | `path` | `["id"]` |
| Query parameter `?page=` | `query` | `["page"]` |
| Header parameter `X-Org` | `header` | `["X-Org"]` |
| Cookie parameter `session` | `cookie` | `["session"]` |
| Body field `user.name` | `body_field` | `["user", "name"]` |
| Entire request body | `body_root` | `["body"]` |

Each binding also records `style` and `explode` for correct serialization of arrays and objects.

### Simplification rules

Five rules reduce the argument surface area to what an LLM can use reliably:

1. **Inline flat bodies** — if the request body is `application/json` with a top-level object schema, each property is promoted to a top-level LLM argument (bound as `body_field`). This makes individual fields directly addressable rather than requiring the LLM to construct a nested JSON blob.

2. **Preserve complex bodies** — if the body schema is not a flat object (an array, a `oneOf`, a freeform dict), it is exposed as a single `body` argument (bound as `body_root`). The LLM receives the full schema and must supply the value directly.

3. **Strip read-only properties** — schema properties marked `readOnly: true` are removed from the LLM-facing input schema. They cannot be set by a caller and would only confuse the LLM.

4. **Default content type selection** — if a request body offers multiple content types, the first one is selected. The LLM always sends one content type per call.

5. **Binary response detection** — responses with binary content types (`application/octet-stream`, `image/*`, `application/pdf`, etc.) are flagged so the dispatcher can handle them appropriately rather than trying to decode them as text.

### Description generation

If the operation has a `summary` or `description`, it is used as the tool description. If neither is present, a minimal description is synthesized from the operation ID and method. Per-operation config overrides (`description` key) are applied later in the Expose stage.

---

## Stage 4 — Expose (`src/specmcp/core/expose.py`)

**Input:** `list[SimplifiedOperation]`, optional `Config`

**Output:** `ToolRegistry` — an immutable registry of `ToolDefinition` objects.

**What it does:**

- Applies per-operation config overrides: `rename` changes the tool name, `description` replaces the generated description, `hide: true` removes the operation entirely, `additional_properties_strict: true` adds `"additionalProperties": false` to the input schema.
- Builds a fast `name → ToolDefinition` lookup index.
- Provides `list_tools()` (for the MCP `tools/list` handler) and `lookup(name)` (for dispatch).

The registry is built **once at startup** and held as an immutable value for the server's lifetime. Hot-reload (planned for v1.1) will replace it atomically by building a new instance and swapping the reference.

---

## Stage 5 — MCP Server (`src/specmcp/cli/serve.py`)

**Input:** `ToolRegistry`, auth config, dispatch config, transport choice.

**What it does:**

Starts an MCP server using the `mcp` Python SDK with two handlers:

- **`tools/list`** — returns `registry.list_tools()`, the full tool catalog.
- **`tools/call`** — resolves the tool from the registry, calls `dispatch()`, and returns the result as MCP `TextContent` blocks.

An `HttpClient` (`httpx.AsyncClient`) is opened **once** and shared across all tool calls so that TCP and TLS connections are pooled. A new client is **never** opened per request.

An `AuthInjector` is resolved at startup. If any required credentials are missing from the environment, the server exits immediately with a clear error rather than failing silently on the first authenticated request.

The default transport is `stdio`, which is what Claude Desktop and most MCP hosts expect.

---

## Runtime Dispatch (`src/specmcp/runtime/dispatcher.py`)

For each `tools/call` request, the dispatcher executes four steps:

1. **Validate** — re-validates LLM arguments against the tool's input schema using `jsonschema`. This is defence-in-depth; the MCP SDK validates first, but specmcp validates again to catch schema drift between the registry and the SDK's copy.

2. **Build request parts** — walks the `ArgumentMap` and serializes each argument into the correct HTTP location: path variable, query string, header, cookie, or JSON body field. OpenAPI `style` and `explode` semantics are applied during serialization.

3. **Inject auth** — calls `AuthInjector.inject()`, which adds credentials to headers or query params based on the operation's security requirements. The injector evaluates the OR-group logic: if the operation accepts multiple auth schemes, it picks the first configured one.

4. **HTTP call and response formatting** — sends the request via `HttpClient`, which handles retries (configurable), response size guards (truncation at `max_response_bytes`), and timeout. The response body is pretty-printed as JSON if parseable, or returned as raw text. A `[Response truncated]` marker is appended if the response exceeded the size limit.

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Proxy mode (no codegen) | The tool stays in sync with spec changes without rebuilding anything. |
| ArgumentMap as explicit mapping | Makes the Simplify → Dispatch contract testable and auditable. The round-trip invariant: any args dict valid against `llm_input_schema` must produce a complete HTTP request. |
| `SensitiveStr` for credentials | Credentials never appear in logs, tracebacks, or `repr()` output. `reveal()` is the only escape hatch, called only at the injection point. |
| `trust_env=False` on HttpClient | Prevents proxy env vars from silently re-routing API traffic in production. |
| anyio for async | Compatible with both asyncio and trio; the MCP SDK uses anyio internally. |
| Single HttpClient for server lifetime | Connection pooling across all tool calls — opening a new client per call would pay a full TCP+TLS handshake on every request. |
