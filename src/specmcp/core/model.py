"""
specmcp Operation Model — the single canonical internal representation.

All pipeline stages (Normalize → Simplify → Expose) share this model.
No business logic here — pure data.

Round-trip invariant (must hold at all times):
    For any Operation ``op`` and any valid set of LLM arguments ``args``
    accepted by Expose(Simplify(op)).inputSchema, the Dispatcher must be
    able to construct a complete, well-formed HTTP request using only
    ``op`` (the Operation Model) and ``args``.  Simplify must never strip
    information the Dispatcher needs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
ParamLocation = Literal["path", "query", "header", "cookie"]
ParamStyle = Literal[
    # path styles
    "simple", "label", "matrix",
    # query styles
    "form", "spaceDelimited", "pipeDelimited", "deepObject",
]

# ---------------------------------------------------------------------------
# Parameter
# ---------------------------------------------------------------------------


class Parameter(BaseModel):
    name: str
    location: ParamLocation
    required: bool
    schema_: dict[str, Any] = Field(alias="schema_")

    #: OpenAPI serialization style (defaults depend on location)
    style: ParamStyle | None = None
    #: Whether array/object values are exploded (defaults depend on style)
    explode: bool | None = None

    allow_empty_value: bool = False
    deprecated: bool = False

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class RequestBodyVariant(BaseModel):
    """One content-type representation of the request body."""

    content_type: str  # e.g. "application/json", "multipart/form-data"
    schema_: dict[str, Any] = Field(alias="schema_")
    #: Per-field encoding hints for multipart/form-data
    encoding: dict[str, dict[str, Any]] | None = None

    model_config = {"populate_by_name": True}


class RequestBody(BaseModel):
    required: bool
    #: Ordered list of content-type representations.
    #: v1 dispatches variants[0] by default; config can pin a different one.
    variants: list[RequestBodyVariant]


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class ResponseVariant(BaseModel):
    """One content-type representation of a response."""

    content_type: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema_")
    #: True when the content type indicates binary (signals MCP resource mapping)
    is_binary: bool = False

    model_config = {"populate_by_name": True}


class Response(BaseModel):
    #: "200", "2XX", "default", etc.
    status_code: str
    description: str | None = None
    #: Response headers (for future use)
    headers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    variants: list[ResponseVariant] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class AuthRequirement(BaseModel):
    """Reference to a securityScheme by name + the scopes the operation requires."""

    scheme_name: str
    scopes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Operation (core model)
# ---------------------------------------------------------------------------


class Operation(BaseModel):
    """Canonical representation of one REST operation.

    This is the output of the Normalize stage and the input to Simplify/Expose.
    It must contain every piece of information the Dispatcher needs to construct
    a valid HTTP request.
    """

    #: Stable, derived tool-name seed. Collisions resolved deterministically.
    id: str
    method: HttpMethod
    path: str  # e.g. "/users/{id}"

    #: Resolved base URL (no server variables).
    server_url: str
    #: Server variables that were resolved (for transparency in inspect output).
    server_variables_resolved: dict[str, str] = Field(default_factory=dict)

    #: Merged path- and operation-level parameters, deduplicated.
    parameters: list[Parameter]
    request_body: RequestBody | None = None

    #: Ordered: 2xx first, then other status codes, "default" last.
    responses: list[Response]

    #: Outer list = OR (any one set of requirements is sufficient).
    #: Inner list = AND (all requirements in the set must be met).
    auth: list[list[AuthRequirement]] = Field(default_factory=list)

    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False

    #: (file, line) for error messages and inspect output.
    source_location: tuple[str, int] | None = None

    #: x-* vendor extensions, preserved for inspect/codegen.
    vendor_extensions: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Simplify output
# ---------------------------------------------------------------------------


class ArgumentBinding(BaseModel):
    """Maps one LLM-facing argument key to its destination in the HTTP request."""

    source_llm_key: str  # what the LLM sees

    #: Where in the HTTP request this argument goes.
    target_kind: Literal["path", "query", "header", "cookie", "body_field", "body_root"]

    #: JSON-pointer-like path into the dispatch destination.
    #: For "path": ["petId"] means fill {petId} in the URL template.
    #: For "body_field": ["data", "name"] means request_body["data"]["name"].
    target_path: list[str]

    #: Carried from Parameter for serialization in the dispatcher.
    style: ParamStyle | None = None
    explode: bool | None = None


class ArgumentMap(BaseModel):
    """Explicit mapping from every LLM-facing argument to its HTTP destination.

    This is the single source of truth for how LLM args become HTTP request parts.
    No implicit magic in the Dispatcher — it follows the ArgumentMap exactly.
    """

    bindings: dict[str, ArgumentBinding]


class SimplifyWarning(BaseModel):
    """A warning emitted by the Simplify stage for user attention."""

    kind: Literal[
        "fallback_to_freeform",       # schema too complex; fell back to single string arg
        "union_collapsed_lossy",       # oneOf/anyOf collapsed but some branch info lost
        "recursive_schema_detected",   # schema contains cycles; cycles broken
        "description_truncated",       # description was truncated
        "unsupported_style",           # parameter style not fully supported
    ]
    operation_id: str
    field_path: str | None = None     # JSON-pointer path to the offending field
    message: str


class SimplifiedOperation(BaseModel):
    """Output of the Simplify stage.

    Carries both the full-fidelity Operation (for dispatch) and the
    LLM-facing projection (for tool schema exposure).
    """

    #: Full-fidelity operation — the Dispatcher reads this.
    operation: Operation

    #: What the LLM sees as the tool input schema (JSON Schema).
    llm_input_schema: dict[str, Any]

    #: Tool description shown to the LLM (truncated, possibly synthesized).
    llm_description: str

    #: How LLM args are routed to HTTP request parts.
    arg_map: ArgumentMap

    #: Warnings for the user (inspect output, validate warnings).
    warnings: list[SimplifyWarning] = Field(default_factory=list)
