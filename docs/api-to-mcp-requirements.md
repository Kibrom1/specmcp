# Requirements: specmcp — API Spec → MCP Server Conversion Tool

**Status:** Draft v0.3
**Tool name:** `specmcp`
**Primary user (v1):** External developers (OSS / product builders)
**Future users:** API platform teams (v2), non-technical users via hosted UI (v3)

---

## 1. Problem & Opportunity

### 1.1 Problem
Developers who want their API consumable by LLM agents via MCP today have three options, all bad:

- **Hand-write the MCP server.** For an API of any size this is hundreds to thousands of lines of repetitive glue: parameter parsing, schema translation, auth handling, error mapping. It also has to be maintained as the API evolves.
- **Use a partial community converter.** Several open-source OpenAPI-to-MCP scripts exist; most cover a narrow slice of the spec, produce unidiomatic output, and break on real-world APIs (OAuth, refs, polymorphism, large specs).
- **Skip MCP entirely** and rely on tool-use prompting against raw HTTP, which is brittle and doesn't compose with the broader MCP ecosystem.

### 1.2 Cost of the status quo
Every API team that wants LLM access pays the same integration tax. For a 200-operation API, hand-rolling an MCP server is 1–3 engineer-weeks of work plus ongoing maintenance. This cost is the main reason most APIs don't yet have an MCP surface, which in turn limits what agents can actually do.

### 1.3 Opportunity
A tool that reliably converts existing API specs into working MCP servers collapses that 1–3 weeks into minutes. If we can be the default way developers ship MCP for an existing API, we sit at the center of agent-API integration.

### 1.4 Competitive landscape

**Direct competitors (do roughly what specmcp does):**
- **`openapi-mcp` (Tadata Inc., PyPI)** — closest direct competitor. Points at an OpenAPI spec URL and creates an MCP server. Python library-shaped (not CLI-first), framework-agnostic, no codegen mode, no GraphQL or Postman, limited customization surface. Investigate before v1 to refine differentiation.
- **`fastapi-mcp` (Tadata Inc., PyPI)** — narrower scope: a FastAPI extension that exposes endpoints as MCP tools via ASGI. Doesn't convert specs, only works inside a running FastAPI app. Not a direct competitor for the spec-conversion use case but covers part of the same audience.
- **`mcpify` (PyPI)** — auto-detects APIs in existing projects (argparse CLIs, Flask, FastAPI) and exposes them as MCP. Different ingestion approach (project analysis, not spec parsing) but overlapping user intent. The name is taken on PyPI by this tool.
- **FastMCP's OpenAPI support** — Python library, less mature OpenAPI surface than the dedicated `openapi-mcp`; primarily an MCP server framework with OpenAPI as one input.
- **Community OpenAPI→MCP scripts** (various GitHub repos) — narrow coverage, no maintenance commitment, no GraphQL or Postman.

**Adjacent, non-competing:**
- **Speakeasy / Stainless** — focused on client SDK generation, not MCP servers.
- **`spec-driven-mcp`, `spec-workflow-mcp`, `openspec-mcp`** — confusingly similar names but different domain entirely; these manage product/engineering spec documents (requirements, design, tasks) inside a dev workflow, not API specs. We need to differentiate clearly in tagline and docs.

**Hand-rolled** — still the default for most teams.

**Where specmcp wins:**
- CLI-first (not library-shaped), so users can stand up an MCP server without writing or modifying code.
- Both proxy and codegen modes from one tool (v1: proxy, v1.1: codegen).
- Multi-format input planned: OpenAPI + GraphQL + Postman (v1: OpenAPI; v2: GraphQL + Postman).
- Designed against real-world specs (refs, polymorphism, OAuth) rather than toy examples.
- Explicit attention to LLM-usability of generated tools (schema simplification as a first-class feature, not just protocol conformance).

The closest competitor, `openapi-mcp`, occupies the simplest slice of this space — "point at a URL, get a server." specmcp covers the same slice plus the harder cases: customization, complex auth, LLM-usability tuning, and codegen output for users who want to own and customize the server.

## 2. Goals and Non-Goals

### Goals
- Turn any reasonably well-formed API spec into a usable MCP server with minimal manual work.
- Produce output that is readable, debuggable, and customizable.
- Be predictable: the same spec should produce the same server.
- Support both codegen and proxy workflows from a single tool.
- Generate tools that LLMs can actually use well, not just tools that conform to the protocol.

### Non-Goals (v1)
- Hosting or running MCP servers as a managed service.
- A graphical UI (deferred to v3).
- Automatically discovering APIs that lack a spec.
- Generating clients (this is server-side only).
- Plugin system (deferred to v2 once usage patterns are clear).

## 3. Primary User & Use Cases

The v1 target is an external developer who wants to make an existing API available to an LLM via MCP. Typical scenarios:

- A developer has a SaaS product with an OpenAPI spec and wants Claude (or another MCP client) to call it.
- An open-source maintainer wants to ship an MCP server alongside their REST API.
- A developer has a Postman collection from a third-party service and wants quick MCP access without writing glue code.

## 4. Scope and Phasing

A core review finding was that v1 scope was too broad. Revised phasing:

### v1 (target: 8 weeks to GA)
- **Spec input:** OpenAPI 3.0 and 3.1 only.
- **Output:** Proxy mode only (no codegen yet).
- **Auth:** API key (header/query) and Bearer token only.
- **Distribution:** PyPI package + prebuilt binaries.
- **Platforms:** macOS and Linux.

Rationale: proxy mode is faster to ship, easier to iterate, and lets us learn what specs break in the wild before we commit to codegen output formats. OpenAPI 3 covers the majority of target APIs. API key and Bearer cover ~70% of public APIs without the OAuth complexity.

### v1.1 (target: +6 weeks)
- Codegen mode in one language (TypeScript or Python — decided by v1 user research).
- Swagger 2.0 input with auto-upgrade.
- Basic auth.
- Windows support.

### v2 (target: +3 months from v1)
- GraphQL input.
- Postman collection input.
- OAuth 2.0 (authorization code + client credentials).
- Second codegen language.
- Plugin interface.

### v3+
- Hosted UI for non-technical users.
- API platform team features (CI integration, governance, diff reports).

The rest of this document describes the **full eventual scope**, with each requirement tagged by priority (**P0** = required for v1, **P1** = v1.1, **P2** = v2, **P3** = later).

## 5. Functional Requirements

### 5.1 Spec ingestion
- **[P0]** Accept OpenAPI 3.0 and 3.1 (YAML or JSON), from local file paths and remote URLs.
- **[P0]** Validate the spec on load and report errors with line/path references.
- **[P0]** Support `$ref` resolution including external refs and circular refs.
- **[P1]** Accept Swagger 2.0 with automatic upgrade to OpenAPI 3.
- **[P2]** Accept GraphQL schemas (SDL `.graphql` files and introspection JSON).
- **[P2]** Accept Postman collections v2.1.

### 5.2 Operation → tool mapping
- **[P0]** Each REST operation becomes one MCP tool by default.
- **[P0]** Tool name derived deterministically from operation ID, falling back to a documented algorithm based on path + method. Naming algorithm must be in the docs.
- **[P0]** Tool description sourced from spec descriptions/summaries; fall back to a generated stub if missing.
- **[P0]** Input schema generated from spec parameters and request body, expressed as JSON Schema.
- **[P0]** Allow per-operation overrides via config (rename, hide, redescribe, regroup).
- **[P0]** Schema simplification pass for LLM usability: flatten one-of/any-of where possible, inline shallow refs, drop spec-only metadata. Behavior is documented and configurable. *(This is a real product question, not a side feature — see §11.)*

### 5.3 Resources vs tools
- **[P1]** Tools-only in v1. The resource-vs-tool decision is a design question, not a config flag, and needs validation with real users before we commit. Tracked in §11.

### 5.4 Authentication
- **[P0]** API key (header, query, cookie).
- **[P0]** Bearer tokens.
- **[P1]** Basic auth.
- **[P2]** OAuth 2.0 (authorization code, client credentials, PKCE, token refresh, pluggable token store). OAuth is deferred because realistic OAuth support — refresh, multi-tenant token storage, scope mapping — is roughly 30% of total engineering work and warrants its own design doc.
- **[P0]** All auth values come from environment variables or a config file. Tool refuses to write secrets to generated files or logs.

### 5.5 Output modes

**Proxy mode [P0]**
- A binary or package that reads a spec at startup and serves MCP without codegen.
- Configuration via YAML/JSON pointing at the spec, auth, and overrides.
- `--dry-run` flag prints the tools that would be exposed without serving, for inspection.
- **[P1]** Hot-reload on spec change.

**Codegen mode [P1]**
- Generate a standalone server project in one language for v1.1 (language chosen via v1 user research).
- Output includes: server entrypoint, tool handlers, types, README, lockfile, `.env.example`.
- Generated code is human-readable and idiomatic, not a single megafile.
- **Regeneration policy: destructive by default.** Re-running codegen into a clean directory produces identical output. Customization happens through explicit extension points (config + override files), not by editing generated source. *(Decision per review — preserving edits across regen is a tar pit; we follow the Prisma/OpenAPI Generator pattern.)*
- **[P2]** A second language.

### 5.6 Customization
- **[P0]** A config file (`mcp.config.yaml`) defines: spec source, auth, naming rules, included/excluded operations, per-operation overrides, base URL, custom headers.
- **[P0]** All config options also expressible via CLI flags for scripting.
- **[P2]** User-overridable codegen templates.

### 5.7 CLI
- **[P0]** `specmcp init` — scaffold a config from a spec.
- **[P0]** `specmcp serve` — run in proxy mode.
- **[P0]** `specmcp validate` — check the spec and config without serving.
- **[P0]** `specmcp inspect` — list the tools that would be exposed, with schemas, in human-readable form.
- **[P1]** `specmcp generate` — produce server code.
- **[P0]** Sensible exit codes and machine-readable output (`--json`) for CI.

## 6. Developer Experience Requirements

For an external-developer audience, DX is the product. These are P0 unless noted.

- **First-run experience:** from `pipx install specmcp` to a working proxy server pointing at a real spec in under five minutes, following only the README.
- **Error messages:** every failure mode produces an error that tells the user (a) what happened, (b) where in the spec or config it happened, (c) what to try. No stack traces in user-facing output unless `--verbose`.
- **Inspection:** `inspect` command shows the user exactly what the LLM will see — tool names, descriptions, schemas — before they connect a client.
- **Examples:** ship with at least three runnable examples against real public APIs (e.g. a simple weather API, a paginated CRUD API, a complex API with refs and polymorphism).
- **README quality:** generated projects (P1) include a README covering setup, auth configuration, running the server, customizing tools, and known limitations.
- **Onboarding doc:** the main project docs include a 10-minute tutorial that takes a new user from zero to a working MCP server connected to a client.

## 7. Non-Functional Requirements

### 7.1 Performance [P0]
- Start a proxy server for a Stripe-sized spec (~450 operations) in under 5 seconds on a developer laptop. Target sizes calibrated against real APIs: Stripe ~450, GitHub ~900, Linear ~120.
- Per-call overhead in proxy mode under 50ms p95 excluding upstream latency.
- **[P1]** Same targets for codegen.

### 7.2 Reliability [P0]
- Spec parsing errors never crash the tool; always reported cleanly.
- Proxy servers handle upstream API errors and return MCP-formatted error responses.
- Graceful behavior on partially valid specs: skip and warn rather than abort, with `--strict` to invert.

### 7.3 Security [P0]
- No spec content or auth credentials sent to any third-party service. Telemetry (§9) is opt-in and never includes spec contents.
- Secrets loaded from environment variables only; refuse to write secrets to generated files or logs.
- Generated/proxied servers validate inputs against the JSON Schema before calling upstream.
- Security review required before v1 GA, focused on OAuth handling (when added), token storage, and proxy-mode request forwarding.

### 7.4 Extensibility
- **[P2]** Plugin interface for custom auth, naming rules, and request/response transforms.
- Until then, customization happens through config + override files only.

### 7.5 Compatibility
- **[P0]** macOS and Linux.
- **[P1]** Windows.
- **[P1]** Generated Python supports 3.10+; generated TypeScript supports Node 20+.
- **[P0]** Tracks the current MCP spec version with a written policy for handling MCP version updates (see §10 risks).

### 7.6 Observability [P0]
- Structured logs (JSON) with `--verbose`.
- Per-tool-invocation logs with redacted secrets.
- Anonymous, opt-in usage telemetry (§9).

## 8. Distribution, Licensing, and Pricing

- **License:** Apache 2.0. Permissive enough for commercial use, with patent protection.
- **Distribution:** PyPI package (`specmcp`), installable via `pip install specmcp` or `pipx install specmcp`; prebuilt standalone binaries for macOS and Linux via GitHub Releases. (See engineering design doc for builder choice — `shiv` provisional.)
- **Pricing:** free and open-source for v1. A paid hosted/managed tier is possible in v3+ but is not a v1 consideration. Architectural decisions should not assume a paid tier exists.
- **Generated code licensing:** generated code carries no license restrictions from this tool; users own their output.

## 9. Telemetry and Feedback Loop

Without telemetry, we can't tell what works in the wild. With sloppy telemetry, we lose trust.

- **[P0]** Opt-in anonymous usage telemetry, disabled by default, enabled via a clearly documented flag or env var.
- **[P0]** When enabled, telemetry includes: tool version, OS, command run, spec size (operation count only), success/failure, anonymized error categories. **Never** includes spec contents, URLs, schemas, auth, or user identifiers.
- **[P0]** Crash reports follow the same opt-in model.
- **[P0]** A public `STATUS.md` or dashboard summarizing aggregate telemetry monthly, so users know what we collect and what we learn.
- **[P0]** A `report-issue` CLI command that gathers (with consent) a sanitized bundle of config + sanitized spec excerpt for bug reports.

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MCP spec evolves and breaks compatibility | High | High | Pin to a specific MCP spec version; CI tests against the published conformance suite; document version policy. |
| Auto-generated tools are unusable by LLMs because schemas are too complex | High | High | Treat schema simplification (§5.2) as a real feature with its own design. Validate v1 against real LLM tool-use on target APIs. |
| OAuth implementation underestimates complexity | High | Medium | Defer to v2 with its own design doc. Don't promise OAuth in v1. |
| Proxy mode introduces rate-limit / quota issues upstream | Medium | Medium | Built-in client-side rate limit awareness, per-tool concurrency limits, clear docs on the user's responsibility for upstream quotas. |
| Security review surfaces issues with proxy forwarding | Medium | High | Security review scheduled before GA; threat model documented; pen-test for the proxy code path. |
| Real-world specs (polymorphism, refs, vendor extensions) break the converter | High | Medium | Test corpus of 10+ real public APIs in CI before GA. Track conversion success rate as a release metric. |
| Competitor (FastMCP or new entrant) ships a comparable tool first | Medium | Medium | Move fast on v1; differentiate on multi-format input, both modes, and LLM-usability focus. |

## 11. Open Questions

- **Schema simplification depth.** How aggressively should we flatten/simplify nested schemas? Too little and LLMs can't use the tools; too much and we lose fidelity. Needs a design doc and validation against real LLM tool-use, not just a config flag.
- **Tools vs resources mapping.** When should a GET endpoint surface as an MCP resource rather than a tool? Different semantics for the LLM. Needs user research before we commit to either default.
- **Large APIs (1000+ operations).** Single server or split by tag/namespace? Affects how the LLM picks tools at runtime.
- **Spec change handling.** Diff report, regenerate-and-merge, or both? Likely a v2 conversation, but the v1 codegen model affects what's possible later.
- **Postman test scripts.** Ignore, translate, or warn? (v2 question.)
- **Codegen language choice for v1.1.** TypeScript or Python? Decide via v1 user research.

## 12. Success Metrics

A successful v1 means:

### Adoption (outcome metrics)
- **500+ weekly active installs** within 3 months of GA.
- **At least 20 GitHub stars per week** average in the first quarter (signal of OSS momentum).
- **At least 5 third-party blog posts, tutorials, or demos** from external developers in the first quarter.

### Quality (output metrics)
- **80%+ conversion success rate** across a fixed test corpus of 15 real-world public API specs, measured at each release.
- **100% conformance** with the targeted MCP spec version's protocol tests.
- **Time-to-first-server under 5 minutes** for a new user following the README, measured via timed user tests with at least 5 external developers before GA.

### Retention (outcome metrics)
- **>40% of users** who run `init` also run `serve` within 7 days (activation funnel).
- **>25% of installs** active 30 days later (rough retention proxy via telemetry, opt-in).

### Qualitative
- Net positive sentiment on GitHub issues, Discord, and developer social channels — measured by a quarterly review of feedback.
- Would-recommend score from a survey of 20+ early users: target 7/10 or higher.

### Kill criteria
If, 3 months post-GA, weekly installs are under 100 and the conversion success rate is below 60%, we reassess the project rather than continue building v2.

## 13. Constraints & Assumptions

- The input spec is the source of truth; the tool does not invent operations not in the spec.
- For GraphQL (v2), each query and mutation becomes a separate tool; subscriptions are out of scope.
- For Postman (v2), environment variables in the collection map to MCP server config variables.
- We assume the MCP spec remains roughly stable in shape over the v1 timeframe; breaking changes trigger the policy in §10.

## 14. Out of Scope (Explicit)

- Hosted SaaS offering (v3+).
- MCP client implementations.
- Auto-generating API specs from running APIs.
- Non-HTTP transports (gRPC, WebSocket) — possible v2+.
- AI-assisted spec authoring or repair.

---

## Appendix: Changelog

### v0.3 (current)
- Chose name: **specmcp** (verified available on PyPI; bare slot also looks open on npm).
- Updated CLI commands throughout from `mcp-from-api` to `specmcp`.
- Switched distribution from npm-first to PyPI-first (aligns with Python implementation decision in design doc).
- Expanded competitive landscape (§1.4): added `openapi-mcp` (Tadata) as the closest direct competitor, `fastapi-mcp` and `mcpify` as overlapping projects, and acknowledged the naming-adjacent `spec-driven-mcp` ecosystem we'll need to differentiate from in marketing.

### v0.2
- Added problem statement, cost of status quo, competitive landscape (§1).
- Added explicit scope phasing with v1 / v1.1 / v2 / v3 split (§4).
- Tagged every requirement with priority (P0–P3).
- Cut v1 scope significantly: OpenAPI 3 only, proxy only, API key + Bearer only, macOS/Linux only.
- Resolved the regeneration-policy question: destructive regen, no edit preservation (§5.5).
- Moved schema simplification from a side feature to a tracked product question (§5.2, §11).
- Added Developer Experience section (§6).
- Added Distribution, Licensing, and Pricing (§8).
- Added Telemetry section with privacy guarantees (§9).
- Added Risks table (§10).
- Replaced thin success criteria with outcome-based metrics including kill criteria (§12).
- Performance targets justified against real API sizes (§7.1).
- Removed plugin interface from v1 (deferred to v2).
