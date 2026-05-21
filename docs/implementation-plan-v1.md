# specmcp v1 — Implementation Plan

**Author:** Staff Engineer review  
**Based on:** `api-to-mcp-requirements.md` v0.3, `api-to-mcp-design-v1.md` v0.4  
**Target:** v1 GA in 8 weeks  
**Status:** Draft for team alignment

---

## 0. How to Read This Document

This plan translates the requirements and design docs into an ordered, week-by-week build sequence. It calls out the critical path, identifies blockers that must be resolved _before_ code is written, flags design gaps, and states the non-obvious judgement calls made at each milestone. The goal is that any engineer can pick it up and know exactly what to build next, why, and in what order.

---

## 1. Pre-Implementation Blockers (Resolve Before Week 1 Kicks Off)

These are not implementation tasks — they are open decisions in the design docs that will force costly rework if left unresolved and discovered mid-sprint.

### 1.1 Simplify Stage Design Doc (CRITICAL — Hard Blocker)

The design doc explicitly states: _"This stage gets its own design doc before implementation."_  
The Simplify stage is also identified as having the **highest product risk** ("auto-generated tools are unusable by LLMs because schemas are too complex" — High/High risk in §10 of requirements).

**What must be resolved:**
- The full set of `SimplifyWarning` kinds and their user-visible messages.
- Decision trees for non-obvious cases: deep `allOf` chains, `discriminator`-tagged unions, mixed enum + freeform strings.
- Corpus-validated defaults for the five simplification rules.
- Exact behavior when a schema cannot be simplified (the "free-form JSON fallback" path — what does the LLM actually see? What warning fires?).
- The ArgumentMap construction algorithm for each simplification rule, particularly for `collapse_unions` where the LLM arg must be routed to the correct `oneOf` branch at dispatch time.

**Owner:** Lead engineer + product, 2–3 days before Week 1 code starts.

### 1.2 Build Tooling Choice

The design doc lists this as an open question: `uv` vs `hatch`.  
**Recommendation:** Lock in `uv` now. It is faster, its lockfile format is stable, and the project structure is simple enough that `hatch`'s extensibility buys nothing. This decision affects `pyproject.toml` structure, CI scripts, and the binary-build pipeline. Changing it mid-project is a day of churn.

### 1.3 JSON Schema Validator Choice

`jsonschema` (rich errors) vs `fastjsonschema` (5–10× faster, worse errors).  
**Recommendation:** Start with `jsonschema`. The performance budget in §9 of the design doc allocates 8ms for argument validation per call, which `jsonschema` can hit for the schemas we'll see in practice. Profile at the Week 4 milestone (first working `serve`). Switch to `fastjsonschema` only if profiling shows it as a bottleneck.  
Lock this in now so the team doesn't revisit it every sprint.

### 1.4 Test Corpus Manifest Format

The design doc says the corpus is a `test-corpus/manifest.yaml` listing URLs, expected SHA256, and license notes. The format is not defined. It must be designed before integration tests are scaffolded (Week 2). Define it as a simple YAML schema and check it into `docs/` before code lands.

### 1.5 Telemetry Endpoint

The design doc describes the telemetry system but does not specify the endpoint URL or the backend it posts to. The telemetry client needs a compile-time constant for this. Decision: either define the endpoint or confirm telemetry is a stub (disabled, no-op) in v1 that never actually sends. Either is fine; the code path is the same. Decide before Week 5.

---

## 2. Repository Setup (Day 1 — Before Any Feature Code)

Everything below is pure scaffolding. No feature logic. The goal is that by end of Day 1, a new engineer can clone the repo, run `uv sync`, and have tests pass.

**Checklist:**

- [ ] `pyproject.toml` with `uv`, `src/` layout, Python 3.11+ constraint.
- [ ] `src/specmcp/` package stub (`__init__.py` with `__version__ = "0.1.0.dev0"`).
- [ ] Directory skeleton matching §8 of the design doc:
  ```
  src/specmcp/{cli,core,runtime,auth,telemetry}/
  tests/{unit,integration}/
  test-corpus/
  examples/
  docs/
  design/
  ```
- [ ] `pyproject.toml` dev dependencies: `pytest`, `pytest-asyncio`, `syrupy`, `ruff`, `mypy`.
- [ ] GitHub Actions CI file: lint → typecheck → unit tests → (integration tests, conditional on corpus fetch).
- [ ] Pre-commit hooks: `ruff format`, `ruff check`, `mypy --strict` on `src/`.
- [ ] `CONTRIBUTING.md` with the branching model (conventional commits, semver).
- [ ] Empty `test-corpus/manifest.yaml` with a comment block showing the schema.
- [ ] `docs/naming.md` stub — the naming algorithm document referenced in §4.3. Even a skeleton matters here because the algorithm _must not change without a deprecation cycle_.

**Why this order:** CI must be green before any feature code merges. If scaffolding and CI are done on Day 1, every subsequent PR is automatically gated.

---

## 3. Milestone Breakdown (8 Weeks)

### Milestone 0 — Foundation (Weeks 1–2)

**Goal:** The Operation Model exists, is validated by tests, and error taxonomy is wired up. The `validate` command works end-to-end on the Petstore spec.

**Components to build, in dependency order:**

#### M0.1 — Error Taxonomy (`src/specmcp/errors.py`)

Build the full `SpecmcpError` hierarchy from §4.10 of the design doc _first_, before any other code. Every other component raises these. Building it last means retrofitting error handling everywhere.

- `SpecmcpError` base with `code`, `message`, `detail`, `location`, `request_id`, `context`.
- All subclasses as defined in the taxonomy.
- CLI exit code mapping (the `sysexits.h` table from §4.10).
- MCP error response mapping table (used by the runtime — define it here, reference it there).
- Unit tests: every error class instantiates correctly; the exit code map is exhaustive.

**Estimated effort:** 0.5 days.

#### M0.2 — Config Schema (`src/specmcp/config.py`)

The Pydantic v2 model for `mcp.config.yaml`. Every CLI command loads this.

- Implement the full config schema from §4.11 of the design doc.
- `Config.load(path)` classmethod: reads YAML, validates with Pydantic, raises `ConfigError` on failure.
- `version` field validation: reject unknown versions immediately.
- `value_from: env(VAR_NAME)` parsing — a small DSL that reads env vars at load time, raises `ConfigError` if the var is absent.
- Unit tests: valid config round-trips; missing required fields error; unknown version rejects; missing env var errors at load time with a clear message.

**Note:** The `value_from` env-var DSL is security-critical. Auth secrets must never leak into error messages or logs. Test this explicitly: `ConfigError` messages for a missing env var should include the variable _name_ but never a partial value.

**Estimated effort:** 1 day.

#### M0.3 — Operation Model (`src/specmcp/core/model.py`)

The Pydantic v2 data model from §4.3. This is the foundation of the entire pipeline.

- Implement `Parameter`, `RequestBodyVariant`, `RequestBody`, `ResponseVariant`, `Response`, `AuthRequirement`, `Operation` exactly as specified.
- `SimplifiedOperation` and `ArgumentMap` / `ArgumentBinding` from §4.4.
- `SimplifyWarning` stub (the full set of warning kinds will come from the Simplify design doc, but the structure can be defined now).
- No business logic here — pure data model.
- Unit tests: every model instantiates and round-trips through Pydantic serialization; required fields are enforced.

**Estimated effort:** 1 day.

#### M0.4 — Load + Parse Stage (`src/specmcp/core/load.py`)

Implements §4.2 of the design doc.

- `load_spec(source: str | Path) -> tuple[RawSpec, ResolvedSpec]`:
  - Fetches from local path or URL (`httpx` for remote).
  - Detects format (YAML/JSON, OpenAPI 3.x vs Swagger 2).
  - Raises `SpecUnsupportedError` for Swagger 2.0 with the exact message: _"Swagger 2.0 is not supported in v1; planned for v1.1."_ — do not half-parse it.
  - Uses `ruamel.yaml` for raw parsing (line/column tracking).
  - Uses `prance` for ref resolution into the resolved view.
  - Uses `openapi-spec-validator` for meta-schema validation.
  - Raises the appropriate `SpecError` subclass for each failure mode.
- Keep the `prance` call behind a `SpecResolver` wrapper class with a single method. This is the **swap point** if prance proves problematic in the test corpus.
- Unit tests: local YAML, local JSON, remote URL (mocked with `httpx` respx), Swagger 2.0 rejection, malformed YAML, unresolvable `$ref`, circular ref detection.

**Estimated effort:** 2 days (prance edge cases take time).

#### M0.5 — Normalize Stage (`src/specmcp/core/normalize.py`)

Implements §4.3. Takes the resolved spec, emits a list of `Operation` objects.

- OpenAPI 3.0→3.1 normalization rules:
  - `nullable: true` → `type: [..., "null"]`
  - `exclusiveMaximum: true` (boolean) → `exclusiveMaximum: <value>` (numeric)
  - `example` preserved alongside `examples`
- Operation ID derivation algorithm (§4.3 naming algorithm — document in `docs/naming.md` at this point, not later).
- Collision resolution: `_2`, `_3` suffix in spec order.
- `servers[0]` selection; per-operation server override; server variable resolution (unresolved variables = `ConfigError` / startup error).
- Parameter merging: path-level + operation-level, no duplicates.
- Reserved header stripping (`Content-Type`, `Authorization`, `Accept`) with `--verbose` logging.
- Multiple request body content types: preserve all in `variants`, mark `variants[0]` as the dispatch default.
- Deprecated operations: included by default, flagged with `deprecated=True`.
- Config-level overrides (rename, hide, include/exclude tags and operations): applied at the end of Normalize.
- Unit tests: every normalization rule has a targeted test. The Petstore worked example from §5.3 of the design doc is a required fixture — the test must assert the exact `Operation` object produced.

**Estimated effort:** 3 days (3.0/3.1 edge cases are subtle; collision resolution needs golden-file tests).

#### M0.6 — `validate` Command (`src/specmcp/cli/validate.py`)

The first user-facing command. Runs Load + Normalize, reports errors, exits with the correct code.

- `specmcp validate --spec <path-or-url> [--config <path>] [--json] [--verbose]`
- Human-readable output: error per line with `file:line:message` format.
- `--json` output: structured JSON matching the `SpecmcpError` shape.
- Exit codes per §4.10.
- Integration test: run against Petstore spec (vendored) → exit 0. Run against a deliberately broken spec → exit 65 with the correct error message.

**Estimated effort:** 0.5 days.

#### M0.7 — `init` Command (`src/specmcp/cli/init.py`)

- Algorithm from §4.12: load + validate spec, detect security schemes, generate `mcp.config.yaml` scaffold and `.env.example`.
- `--force` to overwrite.
- Print summary: N operations, M auth schemes, next steps.
- Do not enumerate operations in the config file (per the explicit decision in §4.12).
- Integration test: run against Petstore → assert the generated `mcp.config.yaml` has the correct auth section and the `.env.example` lists `PETSTORE_API_KEY`.

**Estimated effort:** 1 day.

**Milestone 0 gate:** `specmcp validate` and `specmcp init` both work end-to-end against the Petstore spec. CI is green. The naming algorithm is documented in `docs/naming.md`.

---

### Milestone 1 — Simplify + Inspect (Weeks 3–4)

**Goal:** The full pipeline through Expose runs. `inspect` works and produces the exact output described in §5.4–5.5 of the design doc for the Petstore. The Simplify design doc is finalized before this milestone's code is written.

#### M1.1 — Simplify Stage (`src/specmcp/core/simplify.py`)

Implements §4.4. **This is the highest-risk component.**

Build it in sub-steps:

**M1.1a — ArgumentMap builder (no simplifications yet)**  
Before any simplification logic, build the pass-through case: given an `Operation` with no simplifications applied, produce a `SimplifiedOperation` where `llm_input_schema` mirrors the full schema and `arg_map` has a 1:1 binding for every parameter. This gives an end-to-end smoke test for the rest of the pipeline without betting on the simplification logic being correct.

**M1.1b — Simplification rules, one at a time**  
Implement each of the five rules as a pure function `(schema, bindings) → (schema, bindings, warnings)` and compose them:

1. `inline_shallow_refs` — inline refs used once with < 20 properties.
2. `drop_spec_metadata` — strip `example`, `xml`, `externalDocs`, `discriminator`, `x-*`.
3. `collapse_unions` — simplify `oneOf`/`anyOf`; update `arg_map` for branch routing.
4. `flatten_single_property_wrappers` — detect `{ "data": {actual fields} }` patterns, record re-wrapping in `arg_map`.
5. `truncate_description_chars` — cap at 500 chars.

Each rule: unit tests with both a "simplification applies" fixture and a "no-op" fixture. The no-op case is as important as the positive case.

**M1.1c — Fallback path**  
When a schema can't be safely simplified (arbitrary recursion, etc.), produce a single-argument `string` tool with `"Pass the request body as a JSON string"` description. Log a `SimplifyWarning` with the warning kind defined in the Simplify design doc.

**M1.1d — Config-toggleable rules**  
Wire each rule to its `simplify.*` config flag. A rule set to `false` is bypassed; the ArgumentMap reflects that no simplification was applied for that rule.

**Critical invariant test:**  
For every `SimplifiedOperation`, assert the round-trip invariant from §4.3: a valid set of LLM args accepted by `llm_input_schema` must be dispatchable by the ArgumentMap into a complete, well-formed HTTP request using only the `Operation` model. This test must pass for every corpus spec at every release.

**Estimated effort:** 4 days (including the Simplify design doc review).

#### M1.2 — Expose / Tool Registry (`src/specmcp/core/expose.py`)

Implements §4.5. Takes the list of `SimplifiedOperation`, builds the immutable registry.

- `ToolRegistry.build(ops: list[SimplifiedOperation], config: Config) -> ToolRegistry`
- Per-operation overrides (rename, hide, redescribe) applied here.
- `description` assembly: `summary + description`, truncated, `[GET /path]` appended.
- `[DEPRECATED]` prefix for deprecated operations.
- `tools/list` response constructed from the registry.
- Lookup by tool name: `O(1)`, `None` if not found.
- Registry is built once and held immutably. (Hot-reload, P1, will replace it atomically — the registry must be a value, not a singleton, to make this easy later.)
- Unit tests: registry lookup, hide flag, rename, deprecated prefix. Integration test: registry built from Petstore → tool names match §5.5 of the design doc exactly.

**Estimated effort:** 1 day.

#### M1.3 — `inspect` Command (`src/specmcp/cli/inspect.py`)

- Runs the full pipeline (Load → Normalize → Simplify → Expose).
- Human-readable: summary line, then per-tool: name, method+path, description, `inputSchema`, Simplify warnings, auth schemes.
- `--json`: stable, documented JSON schema. Check it into `docs/inspect-output-schema.json` at this milestone.
- Snapshot tests with `syrupy`: run against Petstore → assert exact output. This is the regression guard for the naming algorithm and Simplify defaults.
- Integration test: the Petstore output from `inspect --json` matches the expected tool definition from §5.5 of the design doc exactly (automated assertion, not eyeballing).

**Estimated effort:** 1 day.

**Milestone 1 gate:** `specmcp inspect` against the Petstore produces the exact tool output from §5.5 of the design doc. Syrupy snapshots committed. CI green.

**At this milestone: run a manual LLM-usability test.**  
Feed the `inspect --json` output for the Petstore (and one more complex spec) to Claude and ask it to call the tools. Check that the tool schemas are understandable and that Claude doesn't hallucinate argument names. This is not a CI gate but it's the earliest validation of the most critical product risk. Do not wait until Week 8 for this.

---

### Milestone 2 — MCP Runtime + `serve` (Weeks 4–5)

**Goal:** `specmcp serve` works. A connected MCP client can call tools. The Petstore end-to-end from §5.6 of the design doc succeeds in a live test.

#### M2.1 — Auth Layer (`src/specmcp/auth/`)

Implements §4.7.

- `AuthScheme` Protocol: `apply(request) -> request` and `handle_response(request, response) -> "accept" | "retry"`.
- `ApiKeyAuth`: reads env var at startup, sets header/query/cookie per config. `handle_response` always returns `"accept"`.
- `BearerAuth`: reads env var at startup, sets `Authorization: Bearer <token>`. `handle_response` always returns `"accept"`.
- Auth scheme factory: given a config section, instantiate the correct class. Raise `ConfigError` at startup if the env var is absent (not lazily at call time).
- Auth values are _never_ logged at any level. Implement this as an explicit redaction registry: auth scheme objects know their own sensitive fields.
- Unit tests: both schemes apply correctly to a mock `httpx.Request`; missing env var raises `ConfigError` at construction; auth values are absent from any exception message.

**Estimated effort:** 1 day.

#### M2.2 — HTTP Client (`src/specmcp/runtime/http_client.py`)

Implements §4.8.

- One `httpx.AsyncClient` per upstream host, stored in a dict keyed by netloc. Created at server startup.
- Per-host concurrency semaphore (default 10, configurable).
- Per-call timeout (default 30s, configurable per-operation, hard cap 5 minutes).
- TLS verification on by default; `--insecure` flag logs a startup warning.
- No caching, no automatic retry (per explicit design decision).
- All clients closed cleanly on shutdown.
- Unit tests: host routing (same host reuses client, different host gets a new one); timeout propagates; semaphore blocks beyond per-host limit; TLS flag respected.

**Estimated effort:** 1 day.

#### M2.3 — Tool Dispatcher (`src/specmcp/runtime/dispatcher.py`)

Implements §4.6.1 and §4.6.2. The core of the runtime.

Build in steps:

**M2.3a — Argument-to-request mapping**  
Walk the `ArgumentMap` and construct an `httpx.Request`:
- `path` binding → `style`/`explode`-aware path template filling.
- `query` binding → `form`, `spaceDelimited`, `pipeDelimited`, `deepObject` serialization. `deepObject` (used by Stripe) must be implemented and tested with a Stripe-style fixture.
- `header` binding → set header (blocked for reserved names).
- `cookie` binding → `httpx` cookies arg.
- `body_field` binding → JSON body dict / form data / multipart.
- `body_root` binding → entire arg becomes body root.
- Content-Type and Accept set by dispatcher, not by the ArgumentMap.
- Name-collision handling: the ArgumentMap already resolved this; the dispatcher just follows it.

**M2.3b — Validation step**  
Validate LLM-supplied args against `llm_input_schema` using `jsonschema`. Raise `ArgumentValidationError` with the JSON Schema error path on failure. This runs _before_ any HTTP call.

**M2.3c — Response mapping**  
Implements §4.6.3. The five-rule response mapping (empty body, JSON, text, binary, size cap). The 1 MiB / 256 KiB thresholds must be config-driven constants, not magic numbers.

**M2.3d — Error mapping**  
Implements §4.6.4. Catch every failure mode from §4.10 and map to the MCP error response shape. The error taxonomy table from the design doc (§4.10) is the spec here — implement it as a lookup, not a series of if/else chains.

**M2.3e — Dispatcher loop with AuthScheme**  
The loop: validate → map args → apply auth → send → handle_response → if "retry" re-apply auth → map result. Hard limit of 1 retry per call (even if `handle_response` returns "retry" a second time). This loop shape is designed to accommodate v2 OAuth without modification.

**M2.3f — Logging**  
One structured `structlog` log line per invocation, matching the schema in §4.6.6 exactly. Implement redaction at this layer: argument values never logged at INFO; auth header values never logged at any level. Verify the redaction with a unit test that asserts the log output for a call with auth credentials contains no credential values.

**Estimated effort:** 4 days (argument serialization edge cases are the long tail).

#### M2.4 — Concurrency Model (`src/specmcp/runtime/concurrency.py`)

Implements §4.6.5.

- Global semaphore (default 32).
- Per-host semaphore (default 10) — shared with the HTTP client module.
- Cancellation: `asyncio.CancelledError` propagates; log the cancellation.
- Shutdown: on `SIGINT`/`SIGTERM`, stop accepting new calls, allow 10s grace period, then cancel in-flight and close all clients.
- Unit tests: semaphore blocking; cancellation propagation; graceful shutdown sequence.

**Estimated effort:** 1 day.

#### M2.5 — MCP Protocol Handler + `serve` Command

Integrates the official `mcp` Python SDK.

- Implement the MCP `tools/list` handler: return the registry's tool catalog.
- Implement the `tools/call` handler: invoke the dispatcher, return the result.
- stdio transport (default): wire to the SDK's stdio server.
- HTTP+SSE transport (opt-in `--transport http --port N`): wire to the SDK's HTTP server. Note: the design doc flags HTTP+SSE transport details as still evolving. Pin to the specific MCP SDK version and add a comment flagging this as the upgrade point.
- `specmcp serve --spec <path> [--config <path>] [--transport stdio|http] [--port N]`
- On startup: validate config (fail loud); resolve auth env vars (fail loud); build pipeline; start runtime.
- On `SIGINT`/`SIGTERM`: graceful shutdown per §4.6.5.
- Integration test: start `serve` against the Petstore spec in a subprocess; send the exact `tools/call` JSON from §5.6 of the design doc; assert the exact response. This is the canonical end-to-end test.

**Estimated effort:** 2 days.

**Milestone 2 gate:** The Petstore end-to-end from §5.6 passes as an automated integration test. `serve` starts, accepts a `tools/call`, and returns the correct response. CI green.

---

### Milestone 3 — Hardening + Test Corpus (Weeks 5–6)

**Goal:** specmcp handles real-world specs reliably. Conversion success rate ≥80% across the test corpus. All error paths produce clean, actionable messages.

#### M3.1 — `report-issue` Command

Implements §4.12. Bundles a sanitized debug report:
- Redacted config (env var values omitted entirely, replaced by `"<env-var: NAME>"`).
- Spec excerpt (or full spec stripped of examples and `x-*` extensions).
- Version metadata.
- Last N log lines with argument value redaction.
- Captured exception with full taxonomy.
- User confirmation before writing.
- Output: `specmcp-report-<timestamp>.tar.gz`.

The sanitization logic here is security-critical. Unit test: given a config with a real env var value present, assert the output bundle contains no real values — only the placeholder string.

**Estimated effort:** 1 day.

#### M3.2 — Test Corpus Integration Tests

The corpus manifest (`test-corpus/manifest.yaml`) defines the 15 specs. For each:
- Measure conversion success rate (tools generated / operations in spec).
- Measure fallback count.
- Measure startup time.
- Snapshot the `inspect --json` output for regression detection.

The success rate gate: ≥80% across all corpus specs at every release. Wire this as a CI job that fails if the rate drops below the threshold.

**Priority order for corpus specs** (start with these, add others):
1. Petstore (vendored — already used in earlier milestones).
2. JSONPlaceholder (vendored — simple, good smoke test).
3. Linear ~120 ops (medium complexity).
4. Stripe ~450 ops (this is the performance benchmark spec — **must pass the 5s startup test**).
5. GitHub ~900 ops (large; exercises naming collision resolution heavily).
6. Slack (mixed auth).
7. DigitalOcean (heavy polymorphism — exercises `collapse_unions`).
8. Deliberately broken specs (adversarial cases).

**Stripe spec startup test must run at M3.2.** If it fails the 5s budget, the mitigations from §9 of the design doc (lazy imports, content-hash cache, parallel Simplify) become immediate work items, not deferred ones.

**Estimated effort:** 3 days (corpus fetch, fixture setup, debugging failures).

#### M3.3 — Performance Profiling

Profile `serve` startup on the Stripe spec. Profile per-call overhead on a mock upstream.
- If startup > 5s: implement spec content-hash cache first (highest expected gain — skips prance on repeat starts).
- If per-call overhead > 50ms p95: profile which stage is slow. `jsonschema` validation is the most likely bottleneck; switch to `fastjsonschema` if so.
- Document the profiling results in `docs/performance.md` for future reference.

**Estimated effort:** 1 day.

#### M3.4 — MCP Conformance Tests

Run the MCP SDK's protocol conformance tests against `serve` in CI. 100% conformance is a v1 GA requirement.

**Estimated effort:** 0.5 days (mostly CI wiring).

---

### Milestone 4 — Distribution + DX (Weeks 7–8)

**Goal:** A new developer can follow the README and have a working MCP server in under 5 minutes. Package is publishable to PyPI. Three runnable examples ship.

#### M4.1 — Packaging

- `pyproject.toml` with correct classifiers, entry point (`specmcp = specmcp.cli:app`), and pinned MCP SDK version (surfaced in `specmcp --version`).
- `shiv`-based standalone binary build for macOS (arm64, x64) and Linux (x64, arm64) via GitHub Actions.
- `pipx install specmcp` tested in CI (fresh virtualenv, install, run `specmcp --version`).
- Semver tag automation via conventional commits.
- `CHANGELOG.md` auto-generated.
- Signed GitHub Release with binaries attached.

**Estimated effort:** 2 days.

#### M4.2 — Three Runnable Examples

Per §6 of the requirements doc, ship three examples in `examples/`:

1. **Simple weather API** — a public API with API key auth, minimal complexity.
2. **Paginated CRUD API** — exercises query parameters, shows the LLM doing manual pagination.
3. **Polymorphic API** — exercises `collapse_unions` / `allOf`, shows a non-trivial `inspect` output and how to add per-operation overrides.

Each example includes: a `mcp.config.yaml`, a `.env.example`, a `README.md` covering setup, and a recorded `specmcp inspect` output for reference.

**Estimated effort:** 1.5 days.

#### M4.3 — Documentation

- `README.md`: install, quickstart, auth configuration, commands reference, known limitations.
- `docs/tutorial.md`: 10-minute walkthrough from zero to a working MCP server connected to a client.
- `docs/naming.md`: the full naming algorithm (should already be drafted at M0.5 — finalize here).
- `docs/simplify.md`: the simplification rules, their config flags, the fallback behavior, and how to diagnose `SimplifyWarning`s.
- `docs/inspect-output-schema.json`: the stable JSON schema for `inspect --json` output (should already exist from M1.3 — review and finalize).
- `docs/mcp-version-policy.md`: the version pinning policy from §4.13 of the design doc.

**Estimated effort:** 2 days.

#### M4.4 — Security Review

Required before GA per §7.3 of the requirements doc. Scope:
- Proxy-mode request forwarding (can specmcp be used as an open relay?).
- Auth credential handling (env var loading, redaction in logs and error messages, redaction in `report-issue` output).
- Input validation (LLM-supplied args validated against JSON Schema before upstream call — check bypass paths).
- Telemetry (confirm no spec content, auth, or PII in telemetry payload).

Produce a security review sign-off document. Known-clean is fine; undocumented is not.

**Estimated effort:** 1 day (staff engineer conducts; findings determine whether additional work is needed).

#### M4.5 — LLM-Usability Eval (Pre-GA Gate)

Per §6 of the test strategy in the design doc: for the top 3 corpus APIs, run a fixed eval where Claude is given the generated tools and a set of tasks. Tracked as a release-quality signal.

This is the formal version of the informal check done at the end of Milestone 1. If the Milestone 1 check flagged issues with the Simplify output, they should be fixed before this eval. If this eval finds new issues, they must be triaged: blocker for GA (tool schema is genuinely unusable) vs post-GA improvement.

**Estimated effort:** 0.5 days (eval itself); triage/fixes are separate.

**Milestone 4 gate (= v1 GA):** PyPI package published, binaries on GitHub Releases, README tutorial validated via timed test with at least 5 external developers (< 5 minutes to first working server), 100% MCP conformance, ≥80% corpus conversion rate, security review signed off.

---

## 4. Critical Path

The components that gate everything downstream:

```
Error Taxonomy (M0.1)
  └─▶ Config Schema (M0.2)
        └─▶ Load + Parse (M0.4)
              └─▶ Normalize (M0.5)
                    ├─▶ validate command (M0.6) ← first user-facing output
                    ├─▶ init command (M0.7)
                    └─▶ Simplify (M1.1) ← highest risk; needs Simplify design doc first
                          └─▶ Expose / Tool Registry (M1.2)
                                └─▶ inspect command (M1.3) ← second gate
                                      └─▶ Auth Layer (M2.1)
                                            └─▶ HTTP Client (M2.2)
                                                  └─▶ Tool Dispatcher (M2.3)
                                                        └─▶ serve command (M2.5) ← third gate
                                                              └─▶ Test Corpus (M3.2)
                                                                    └─▶ GA
```

The Operation Model (M0.3) is a dependency of everything above it but is built alongside M0.4. The Auth Layer (M2.1) can be built in parallel with M1.x by a second engineer.

---

## 5. Parallelization Opportunities (If 2+ Engineers)

| Track A | Track B |
|---|---|
| M0.1 Error taxonomy | (wait) |
| M0.2 Config schema | (wait) |
| M0.3–M0.5 Model + Load + Normalize | — |
| M0.6 `validate` | M0.7 `init` |
| M1.1 Simplify | M2.1 Auth + M2.2 HTTP Client |
| M1.2 Expose | M2.3 Dispatcher (using stub registry) |
| M1.3 `inspect` | M2.4 Concurrency |
| M2.5 `serve` (integrate both tracks) | — |
| M3.x Hardening + corpus | — |
| M4.x Distribution + DX | — |

With two engineers, the estimated timeline compresses from 8 weeks to approximately 6 weeks, with the Simplify stage (M1.1) remaining the rate-limiter.

---

## 6. Design Gaps and Concerns (Staff Engineer Flags)

These are items where the design doc either leaves something underspecified or where I have a concern about the approach.

### 6.1 Simplify Stage — Validate Early, Not at GA

The design doc lists LLM-unusable schemas as a High/High risk but defers LLM-usability testing to "before GA." This is too late. By the time we discover the Simplify defaults produce schemas Claude can't parse, we may have the wrong data model or wrong simplification order.

**Recommendation:** Run the informal LLM-usability check at the end of Milestone 1 (after `inspect` works), not at the end of Milestone 4. If the Petstore `inspect` output fed to Claude produces confused tool calls, we know immediately. This costs an hour at M1 and could save a week at M4.

### 6.2 Performance Budget Is Tight

The design doc estimates 4.95s startup for a 450-op spec, leaving 50ms of margin. This margin does not account for:
- Cold start (Python interpreter cache miss on first run after install).
- Network latency for remote spec fetch (unbudgeted).
- Config validation time.

**Recommendation:** Implement the content-hash spec cache (mentioned as a mitigation) as a planned feature in M2/M3, not an emergency mitigation. The cache is straightforward: hash the fetched spec bytes, check for a cached resolved-spec file, load that if present. This makes repeat starts (the common case for a running dev workflow) essentially free.

### 6.3 `prance` Dependency Risk

`prance` is not the most actively maintained library. The design doc acknowledges this with a `SpecResolver` wrapper plan. The wrapper must be built properly at M0.4, not deferred. If prance fails on a corpus spec, the wrapper is the swap point — but only if the interface is clean.

**Recommendation:** Define the `SpecResolver` interface at M0.4 and write at least one test that constructs a `SpecResolver` from a test double (no real prance call). This proves the wrapper is genuinely decoupled.

### 6.4 Naming Algorithm Stability

The design doc says the naming algorithm "must not change without a deprecation cycle." This is easy to commit to in prose and hard to enforce in code. The snapshot tests for `inspect` output are the enforcement mechanism — but only if they're treated as a contract, not a convenience.

**Recommendation:** At M0.5, add a `tests/unit/test_naming_algorithm.py` with golden-file assertions for at least 20 known inputs (covering: clean operationId, missing operationId, collision resolution, special characters in paths, long paths). These golden files are checked into version control and any change to them requires an explicit review comment: "This is a breaking change to the naming algorithm."

### 6.5 `deepObject` Query Parameter Serialization

The design doc calls out `deepObject` (used heavily by Stripe) explicitly. This is a non-trivial serialization: `card[number]=...&card[expiry]=...`. It must be implemented and tested at M2.3 with a Stripe-fixture.

If `deepObject` is missing or wrong, Stripe-based tools will produce malformed requests silently. The ArgumentMap approach means the bug will manifest at the HTTP layer (wrong URL query string), not at validation — hard to debug.

**Recommendation:** Write a unit test for `deepObject` serialization at M2.3a using a fixture extracted from the Stripe spec. Run this test in CI, not just locally.

### 6.6 Auth Secrets — Defense in Depth

The design doc says auth values are "never logged at any level." This is stated as a rule but the enforcement is manual (developers must remember to not log `request.headers["Authorization"]`). Manual rules break under time pressure.

**Recommendation:** Implement a `SensitiveStr` wrapper type (a `str` subclass whose `__repr__` and `__str__` return `"<redacted>"`) and store all auth values as `SensitiveStr` from the moment they're read from the env var. This way, accidental logging or error message inclusion is safe by default, not by discipline.

### 6.7 MCP SDK Version Pinning

The design doc says pin to a specific MCP SDK version and document it. The HTTP+SSE transport is noted as "still evolving." This is a real risk: if the MCP SDK releases a breaking change between M2 and GA, we may need to retrofit.

**Recommendation:** Pin the MCP SDK to a specific minor version (e.g., `mcp>=1.2.0,<1.3.0`) from the first commit. Add an automated check in CI: if the MCP SDK has a new release, open a tracking issue (GitHub Actions can do this). Don't auto-update; upgrade is a deliberate PR with conformance tests.

---

## 7. Testing Strategy Summary

| Test type | Framework | When it runs | Gate |
|---|---|---|---|
| Unit tests | `pytest` + `pytest-asyncio` | Every PR | Blocking |
| Naming algorithm golden files | `pytest` + checked-in fixtures | Every PR | Blocking (diffs require review comment) |
| `inspect` snapshot tests | `syrupy` | Every PR | Blocking |
| Integration tests (Petstore, JSONPlaceholder) | `pytest` (vendored specs) | Every PR | Blocking |
| Integration tests (corpus, 13 fetched specs) | `pytest` (fetched, cached in CI artifact) | Every PR | Blocking (≥80% conversion rate) |
| MCP conformance tests | MCP SDK conformance suite | Every PR | Blocking (100% required) |
| Performance profiling | `pytest-benchmark` or manual | Weekly on main | Non-blocking gate; tracked metric |
| LLM-usability eval | Manual + Claude | End of M1, end of M4 | Pre-GA quality signal |

---

## 8. Week-by-Week Summary

| Week | Milestone | Deliverable |
|---|---|---|
| 1 | M0 setup + M0.1–M0.3 | Repo scaffolded, CI green, error taxonomy + config schema + Operation Model done |
| 2 | M0.4–M0.7 | `validate` and `init` work on Petstore; naming algorithm documented |
| 3 | M1.1 (part 1) | Simplify design doc finalized; pass-through ArgumentMap (no simplifications) working |
| 4 | M1.1 (complete) + M1.2–M1.3 | `inspect` produces exact Petstore output from design doc §5.5; snapshots committed; informal LLM-usability check done |
| 5 | M2.1–M2.4 | Auth layer, HTTP client, dispatcher, concurrency model done |
| 6 | M2.5 + M3.1 | `serve` passes Petstore end-to-end test; `report-issue` done |
| 7 | M3.2–M3.4 | ≥80% corpus conversion; Stripe startup ≤5s; MCP conformance 100% |
| 8 | M4.1–M4.5 | PyPI package, binaries, docs, 3 examples, security review, LLM-usability eval, GA |

---

## 9. Definition of Done for v1 GA

- [ ] All P0 requirements in `api-to-mcp-requirements.md` implemented and tested.
- [ ] `specmcp init`, `validate`, `inspect`, `serve`, `report-issue` all work correctly.
- [ ] ≥80% conversion success rate across the 15-spec test corpus.
- [ ] 100% MCP conformance test pass rate.
- [ ] Stripe-spec startup time ≤5s on a reference developer laptop.
- [ ] Per-call overhead ≤50ms p95 on a mock upstream.
- [ ] All auth values provably absent from logs and error messages (automated test).
- [ ] Naming algorithm documented in `docs/naming.md` and golden-file tested.
- [ ] Simplify rules documented in `docs/simplify.md`.
- [ ] Security review signed off.
- [ ] LLM-usability eval passed (Claude can use the top 3 corpus API tools without hallucinating argument names).
- [ ] `pipx install specmcp` + README tutorial → working server in ≤5 minutes (timed with 5 external developers).
- [ ] PyPI package published; GitHub Release with binaries for macOS arm64, macOS x64, Linux x64, Linux arm64.
- [ ] MCP SDK version surfaced in `specmcp --version`; version policy documented in `docs/mcp-version-policy.md`.

---

*This document should be reviewed by the team before Week 1 begins. The pre-implementation blockers in §1 must be resolved first. Questions and objections should be filed as GitHub issues against this document.*
