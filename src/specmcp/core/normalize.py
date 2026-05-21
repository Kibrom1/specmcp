"""
specmcp Normalize stage.

Takes a ResolvedSpec and produces a list of Operation objects.

Responsibilities:
  - Reduce OpenAPI 3.0 and 3.1 to a single internal Operation representation.
  - Apply the 3.0→3.1 normalization rules (nullable, exclusiveMaximum, etc.).
  - Derive stable, unique operation IDs using the documented naming algorithm.
  - Merge path-level and operation-level parameters (no duplicates).
  - Resolve servers / base URLs.
  - Apply config-level filters (include/exclude tags, operations, deprecated).
  - Strip reserved headers from parameters.

The naming algorithm is documented in docs/naming.md and is stable for v1.
Any change to the algorithm is a breaking change.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from specmcp.core.load import ResolvedSpec
from specmcp.core.model import (
    AuthRequirement,
    HttpMethod,
    Operation,
    Parameter,
    ParamLocation,
    ParamStyle,
    RequestBody,
    RequestBodyVariant,
    Response,
    ResponseVariant,
)
from specmcp.errors import NormalizeError, SourceLocation

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HTTP_METHODS: list[str] = ["get", "put", "post", "patch", "delete", "head", "options"]

#: Headers that are owned by the dispatcher / auth layer.
#: These must never be exposed as LLM-facing tool arguments.
RESERVED_HEADERS = frozenset(
    {"content-type", "authorization", "accept", "content-length", "host", "transfer-encoding"}
)

#: Default parameter styles per location (OpenAPI 3 spec defaults).
DEFAULT_STYLE: dict[ParamLocation, ParamStyle] = {
    "path": "simple",
    "query": "form",
    "header": "simple",
    "cookie": "form",
}

#: Default explode values per style (OpenAPI 3 spec defaults).
DEFAULT_EXPLODE: dict[ParamStyle, bool] = {
    "form": True,
    "simple": False,
    "label": False,
    "matrix": False,
    "spaceDelimited": False,
    "pipeDelimited": False,
    "deepObject": True,
}


# ---------------------------------------------------------------------------
# Naming algorithm  (stable — see docs/naming.md)
# ---------------------------------------------------------------------------


def _sanitize_path_segment(path: str) -> str:
    """Apply the path→name transformation from the naming algorithm.

    Steps:
      1. Replace each '/' with '_'.
      2. Strip '{' and '}' characters (keeps parameter names readable).
      3. Collapse consecutive '_' to a single '_'.
      4. Strip leading/trailing '_'.
    """
    name = path
    name = name.replace("/", "_")
    name = name.replace("{", "").replace("}", "")
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name


def _derive_operation_id(method: str, path: str) -> str:
    """Fallback operation ID derivation when operationId is absent."""
    path_part = _sanitize_path_segment(path)
    if path_part:
        return f"{method.lower()}_{path_part}"
    return method.lower()


def _resolve_collisions(candidates: list[str]) -> list[str]:
    """Given a list of candidate names (in spec order), resolve duplicates.

    The first occurrence keeps the undecorated name.
    Subsequent occurrences get _2, _3, ... suffixes.
    """
    seen: Counter[str] = Counter()
    result: list[str] = []
    for name in candidates:
        if seen[name] == 0:
            result.append(name)
        else:
            result.append(f"{name}_{seen[name] + 1}")
        seen[name] += 1
    return result


# ---------------------------------------------------------------------------
# 3.0 → 3.1 schema normalization
# ---------------------------------------------------------------------------


def _normalize_schema_30_to_31(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single JSON Schema from 3.0 form to 3.1 form.

    Applied recursively to all nested schemas.

    Rules:
      - ``nullable: true`` → add "null" to ``type`` (or wrap in list).
      - ``exclusiveMaximum: true`` (bool) → ``exclusiveMaximum: <value>`` (numeric, 3.1 form).
      - ``exclusiveMinimum: true`` (bool) → ``exclusiveMinimum: <value>`` (numeric, 3.1 form).
      - ``example`` is preserved (kept for 3.0 back-compat; alongside ``examples`` in 3.1).
    """
    if not isinstance(schema, dict):
        return schema

    result = dict(schema)

    # nullable: true → add "null" to type
    if result.pop("nullable", False):
        existing_type = result.get("type")
        if existing_type is None:
            result["type"] = ["null"]
        elif isinstance(existing_type, str):
            result["type"] = [existing_type, "null"]
        elif isinstance(existing_type, list) and "null" not in existing_type:
            result["type"] = existing_type + ["null"]

    # exclusiveMaximum: true (3.0 boolean) → 3.1 numeric
    if result.get("exclusiveMaximum") is True:
        maximum = result.pop("maximum", None)
        result["exclusiveMaximum"] = maximum

    # exclusiveMinimum: true (3.0 boolean) → 3.1 numeric
    if result.get("exclusiveMinimum") is True:
        minimum = result.pop("minimum", None)
        result["exclusiveMinimum"] = minimum

    # Recurse into nested schemas
    for key in ("items", "additionalProperties", "not"):
        if key in result and isinstance(result[key], dict):
            result[key] = _normalize_schema_30_to_31(result[key])

    for key in ("properties", "patternProperties", "definitions", "$defs"):
        if key in result and isinstance(result[key], dict):
            result[key] = {
                k: _normalize_schema_30_to_31(v)
                for k, v in result[key].items()
            }

    for key in ("allOf", "anyOf", "oneOf"):
        if key in result and isinstance(result[key], list):
            result[key] = [_normalize_schema_30_to_31(s) for s in result[key]]

    return result


# ---------------------------------------------------------------------------
# Parameter normalization
# ---------------------------------------------------------------------------


def _normalize_parameter(
    raw: dict[str, Any],
    source: str,
    openapi_version: str,
) -> Parameter | None:
    """Convert a raw OpenAPI parameter dict to a Parameter model.

    Returns None for reserved headers (Content-Type, Authorization, etc.).
    """
    name: str = raw.get("name", "")
    location_raw: str = raw.get("in", "")

    # Validate location
    valid_locations: set[ParamLocation] = {"path", "query", "header", "cookie"}
    if location_raw not in valid_locations:
        raise NormalizeError(f"Parameter '{name}' has unknown 'in' value: {location_raw!r}")

    location: ParamLocation = location_raw  # type: ignore[assignment]

    # Strip reserved headers
    if location == "header" and name.lower() in RESERVED_HEADERS:
        # Design doc: log at --verbose if the spec defined reserved headers
        return None

    schema = raw.get("schema", {})
    if openapi_version == "3.0":
        schema = _normalize_schema_30_to_31(schema)

    style_raw = raw.get("style")
    if style_raw is None:
        style = DEFAULT_STYLE.get(location)
    else:
        style = style_raw  # type: ignore[assignment]

    explode = raw.get("explode")
    if explode is None and style is not None:
        explode = DEFAULT_EXPLODE.get(style)

    return Parameter(
        name=name,
        location=location,
        required=raw.get("required", location == "path"),
        schema_=schema,
        style=style,
        explode=explode,
        allow_empty_value=raw.get("allowEmptyValue", False),
        deprecated=raw.get("deprecated", False),
    )


def _merge_parameters(
    path_params: list[dict[str, Any]],
    op_params: list[dict[str, Any]],
    source: str,
    openapi_version: str,
) -> list[Parameter]:
    """Merge path-level and operation-level parameters.

    Operation-level parameters override path-level ones with the same
    (name, location) pair. Reserved headers are silently dropped.
    """
    # Operation-level wins on (name, in) collision
    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for p in path_params:
        key = (p.get("name", ""), p.get("in", ""))
        combined[key] = p
    for p in op_params:
        key = (p.get("name", ""), p.get("in", ""))
        combined[key] = p

    result: list[Parameter] = []
    for raw in combined.values():
        param = _normalize_parameter(raw, source, openapi_version)
        if param is not None:
            result.append(param)
    return result


# ---------------------------------------------------------------------------
# Request body normalization
# ---------------------------------------------------------------------------


def _normalize_request_body(
    raw: dict[str, Any],
    openapi_version: str,
) -> RequestBody | None:
    if not raw:
        return None

    content = raw.get("content", {})
    variants: list[RequestBodyVariant] = []
    for content_type, media in content.items():
        schema = media.get("schema", {})
        if openapi_version == "3.0":
            schema = _normalize_schema_30_to_31(schema)
        variants.append(
            RequestBodyVariant(
                content_type=content_type,
                schema_=schema,
                encoding=media.get("encoding"),
            )
        )

    if not variants:
        return None

    return RequestBody(required=raw.get("required", False), variants=variants)


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------

_BINARY_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "image/",
        "audio/",
        "video/",
        "application/pdf",
        "application/zip",
        "application/gzip",
    }
)


def _is_binary(content_type: str) -> bool:
    ct = content_type.lower()
    return any(ct.startswith(prefix) for prefix in _BINARY_CONTENT_TYPES)


def _normalize_responses(
    raw_responses: dict[str, Any],
    openapi_version: str,
) -> list[Response]:
    """Normalize response objects.  Order: 2xx first, others, 'default' last."""
    responses: list[Response] = []

    def sort_key(status: str) -> tuple[int, str]:
        if status == "default":
            return (2, status)
        if status.upper().startswith("2") or status.startswith("2"):
            return (0, status)
        return (1, status)

    for status_code, raw in sorted(raw_responses.items(), key=lambda x: sort_key(x[0])):
        content = raw.get("content", {})
        variants: list[ResponseVariant] = []
        for content_type, media in content.items():
            schema = media.get("schema")
            if schema and openapi_version == "3.0":
                schema = _normalize_schema_30_to_31(schema)
            variants.append(
                ResponseVariant(
                    content_type=content_type,
                    schema_=schema,
                    is_binary=_is_binary(content_type),
                )
            )
        responses.append(
            Response(
                status_code=str(status_code),
                description=raw.get("description"),
                headers=raw.get("headers", {}),
                variants=variants,
            )
        )
    return responses


# ---------------------------------------------------------------------------
# Auth normalization
# ---------------------------------------------------------------------------


def _normalize_security(
    security: list[dict[str, list[str]]],
) -> list[list[AuthRequirement]]:
    """Convert OpenAPI security requirement objects to AuthRequirement lists.

    Outer list = OR (any one set is sufficient).
    Inner list = AND (all requirements in a set must be met).
    """
    result: list[list[AuthRequirement]] = []
    for req_obj in security:
        group: list[AuthRequirement] = []
        for scheme_name, scopes in req_obj.items():
            group.append(AuthRequirement(scheme_name=scheme_name, scopes=scopes))
        result.append(group)
    return result


# ---------------------------------------------------------------------------
# Server URL resolution
# ---------------------------------------------------------------------------


def _resolve_server_url(
    servers: list[dict[str, Any]],
    server_variables: dict[str, str] | None = None,
    base_url_override: str | None = None,
) -> str:
    """Pick servers[0] and resolve server variables."""
    if base_url_override:
        return base_url_override.rstrip("/")

    if not servers:
        return ""

    server = servers[0]
    url: str = server.get("url", "")

    # Resolve server variables using defaults or overrides
    for var_name, var_def in server.get("variables", {}).items():
        value = (server_variables or {}).get(var_name) or var_def.get("default", "")
        url = url.replace("{" + var_name + "}", value)

    # If any {variables} remain, that's an error
    unresolved = re.findall(r"\{[^}]+\}", url)
    if unresolved:
        raise NormalizeError(
            f"Server URL has unresolved variables: {unresolved}. "
            "Set defaults in the spec or provide values via config."
        )

    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Main normalize function
# ---------------------------------------------------------------------------


def normalize(
    resolved: ResolvedSpec,
    *,
    base_url_override: str | None = None,
    include_deprecated: bool = True,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    include_operations: list[str] | None = None,
    exclude_operations: list[str] | None = None,
) -> list[Operation]:
    """Convert a ResolvedSpec into a list of Operation objects.

    Parameters
    ----------
    resolved:
        Output of the Load + Parse stage.
    base_url_override:
        If set, replaces the spec's servers[0] URL.
    include_deprecated:
        Whether to include deprecated operations (default True).
    include_tags:
        If non-empty, only include operations with at least one of these tags.
    exclude_tags:
        Operations with any of these tags are excluded.
    include_operations:
        If non-empty, only include operations with these IDs.
    exclude_operations:
        Operations with these IDs are excluded.

    Returns
    -------
    list[Operation]
        One Operation per included REST operation, in spec order.
    """
    spec = resolved.data
    version = resolved.openapi_version
    source = resolved.source

    global_servers: list[dict[str, Any]] = spec.get("servers", [{"url": "/"}])
    global_security: list[dict[str, list[str]]] = spec.get("security", [])
    paths: dict[str, Any] = spec.get("paths", {})

    # --- Pass 1: collect all candidate (id, operation) pairs in spec order ---
    candidates: list[tuple[str, dict[str, Any], dict[str, Any], str, str]] = []
    # (candidate_id, path_item, op_dict, method, path)

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        path_servers = path_item.get("servers", global_servers)
        path_params: list[dict[str, Any]] = path_item.get("parameters", [])

        for method in HTTP_METHODS:
            op_dict = path_item.get(method)
            if not isinstance(op_dict, dict):
                continue

            operation_id = op_dict.get("operationId", "").strip()
            if not operation_id:
                operation_id = _derive_operation_id(method, path_str)

            candidates.append((operation_id, path_item, op_dict, method.upper(), path_str))

    # --- Pass 2: resolve ID collisions in spec order ---
    raw_ids = [c[0] for c in candidates]
    resolved_ids = _resolve_collisions(raw_ids)

    # --- Pass 3: build Operation objects ---
    operations: list[Operation] = []

    for (_, path_item, op_dict, method, path_str), op_id in zip(candidates, resolved_ids):
        # Apply filters
        tags: list[str] = op_dict.get("tags", [])
        deprecated: bool = op_dict.get("deprecated", False)

        if not include_deprecated and deprecated:
            continue
        if include_tags and not any(t in include_tags for t in tags):
            continue
        if exclude_tags and any(t in exclude_tags for t in tags):
            continue
        if include_operations and op_id not in include_operations:
            continue
        if exclude_operations and op_id in exclude_operations:
            continue

        # Server URL
        op_servers = op_dict.get("servers", path_item.get("servers", global_servers))
        try:
            server_url = _resolve_server_url(op_servers, base_url_override=base_url_override)
        except NormalizeError:
            raise

        # Parameters
        path_params: list[dict[str, Any]] = path_item.get("parameters", [])
        op_params: list[dict[str, Any]] = op_dict.get("parameters", [])
        parameters = _merge_parameters(path_params, op_params, source, version)

        # Request body
        request_body = _normalize_request_body(op_dict.get("requestBody", {}), version)

        # Responses
        raw_responses = op_dict.get("responses", {})
        responses = _normalize_responses(raw_responses, version)

        # Auth
        op_security = op_dict.get("security", global_security)
        auth = _normalize_security(op_security)

        # Vendor extensions
        vendor_extensions = {k: v for k, v in op_dict.items() if k.startswith("x-")}

        operations.append(
            Operation(
                id=op_id,
                method=method,  # type: ignore[arg-type]
                path=path_str,
                server_url=server_url,
                parameters=parameters,
                request_body=request_body,
                responses=responses,
                auth=auth,
                summary=op_dict.get("summary"),
                description=op_dict.get("description"),
                tags=tags,
                deprecated=deprecated,
                source_location=(source, 0),  # line resolution requires raw spec
                vendor_extensions=vendor_extensions,
            )
        )

    return operations
