"""
specmcp Tool Dispatcher.

Responsibility: given a ToolDefinition and a dict of LLM-supplied arguments,
construct a complete HTTP request using the ArgumentMap and dispatch it via
the HttpClient.

Design contract (round-trip invariant):
  For any valid LLM args dict accepted by tool.input_schema, the dispatcher
  MUST be able to construct a complete HTTP request. If it cannot, that is a
  bug in the Simplify stage, not a user error.

Dispatcher steps:
  1. Build path — fill {variables} in the path template.
  2. Build query — collect query-bound args.
  3. Build headers — collect header-bound args.
  4. Build body — collect body_field args into a dict; body_root is used directly.
  5. Auth injection — call AuthInjector.inject().
  6. HTTP request — call HttpClient.request().
  7. Return MCP result content blocks.

Serialisation:
  OpenAPI style/explode semantics are implemented for the common cases used by
  real-world APIs. Exotic styles (matrix, label, spaceDelimited, pipeDelimited,
  deepObject) are serialised with a best-effort approach and flagged in the
  SimplifyWarning.kind="unsupported_style" list.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from specmcp.auth.injector import AuthInjector
from specmcp.config import DispatchConfig, OperationOverride
from specmcp.core.expose import ToolDefinition
from specmcp.core.model import ArgumentBinding, Operation
from specmcp.errors import DispatchError, ResponseTooLargeError
from specmcp.runtime.http_client import HttpClient, HttpResponse


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def dispatch(
    *,
    tool: ToolDefinition,
    llm_args: dict[str, Any],
    http_client: HttpClient,
    auth_injector: AuthInjector,
    dispatch_config: DispatchConfig,
    operation_override: OperationOverride | None = None,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """Dispatch a tool call to the upstream API.

    Args:
        tool: The resolved ToolDefinition (from ToolRegistry).
        llm_args: Raw arguments dict from the MCP tools/call request.
        http_client: Initialised HttpClient (managed by serve command).
        auth_injector: Initialised AuthInjector (managed by serve command).
        dispatch_config: Global dispatch settings.
        operation_override: Per-operation overrides from config (timeout, etc.).
        request_id: Correlation ID for log/error attribution.

    Returns:
        MCP content blocks list, e.g. [{"type": "text", "text": "..."}].

    Raises:
        DispatchError: If the ArgumentMap is invalid or arg construction fails
            (indicates a bug in the Simplify stage).
        AuthConfigError: If a required auth scheme is not configured.
        UpstreamClientError / UpstreamServerError / TransientError:
            Propagated from the HttpClient.
    """
    sop = tool.simplified_operation
    op = sop.operation
    arg_map = sop.arg_map

    # 0. Defence-in-depth input validation against the tool's inputSchema.
    #    The MCP SDK validates args, but we re-validate here to catch schema
    #    drift and prevent malformed args from reaching the upstream API.
    _validate_args(tool.name, llm_args, tool.input_schema, request_id=request_id)

    # 1-4. Build request parts from ArgumentMap
    path_vars: dict[str, str] = {}
    query_params: dict[str, str] = {}
    req_headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}
    body_root: Any = _MISSING

    for llm_key, binding in arg_map.bindings.items():
        value = llm_args.get(llm_key, _MISSING)
        if value is _MISSING:
            # Not provided — skip (required check was done by schema validation upstream)
            continue

        kind = binding.target_kind

        if kind == "path":
            path_vars[binding.target_path[0]] = _serialize_path(value, binding)

        elif kind == "query":
            serialised = _serialize_query(llm_key, value, binding)
            query_params.update(serialised)

        elif kind == "header":
            req_headers[binding.target_path[0]] = _serialize_simple(value)

        elif kind == "cookie":
            # Cookies are passed as a Cookie header; AuthInjector already handles
            # cookie-type apiKey schemes. Direct cookie params are rare but valid.
            existing = req_headers.get("Cookie", "")
            pair = f"{binding.target_path[0]}={_serialize_simple(value)}"
            req_headers["Cookie"] = f"{existing}; {pair}" if existing else pair

        elif kind == "body_field":
            _set_nested(body_fields, binding.target_path, value)

        elif kind == "body_root":
            body_root = value

        else:
            raise DispatchError(
                f"Unknown target_kind {kind!r} in ArgumentMap for tool "
                f"{tool.name!r}, key {llm_key!r}. This is a specmcp bug.",
                request_id=request_id,
            )

    # 5. Fill path template
    try:
        filled_path = op.path.format_map(path_vars)
    except KeyError as exc:
        raise DispatchError(
            f"Path variable {exc} missing from ArgumentMap for tool {tool.name!r}. "
            f"This is a specmcp bug.",
            request_id=request_id,
        ) from exc

    url = op.server_url.rstrip("/") + filled_path

    # 6. Determine body
    json_body: Any = None
    form_body: dict[str, Any] | None = None

    if body_root is not _MISSING:
        json_body = body_root
    elif body_fields:
        # Determine content type from the operation's request body
        content_type = _pick_content_type(op)
        if content_type and "form" in content_type:
            form_body = {k: str(v) for k, v in body_fields.items()}
        else:
            json_body = body_fields
            if content_type:
                req_headers.setdefault("Content-Type", content_type)

    # 7. Auth injection (async — OAuth schemes may fetch a token here)
    req_headers, query_params = await auth_injector.inject(
        op.auth,
        headers=req_headers,
        params=query_params,
    )

    # 8. Timeout / retry from override or global config
    timeout = (
        operation_override.timeout_seconds
        if operation_override and operation_override.timeout_seconds is not None
        else dispatch_config.default_timeout_seconds
    )
    from specmcp.config import RetryConfig
    retry = (
        operation_override.retry
        if operation_override and operation_override.retry is not None
        else None
    )

    # 9. HTTP call — streaming or buffered
    if dispatch_config.enable_streaming and _operation_may_stream(op):
        streaming_timeout = timeout * dispatch_config.streaming_timeout_multiplier
        raw_text, truncated = await http_client.stream_request(
            method=op.method,
            url=url,
            headers=req_headers,
            params=query_params,
            json_body=json_body,
            timeout_seconds=streaming_timeout,
            streaming_max_bytes=dispatch_config.streaming_max_bytes,
            request_id=request_id,
        )
        text = raw_text
        if truncated:
            text += "\n\n[Response truncated]"
        return [{"type": "text", "text": text}]

    response = await http_client.request(
        method=op.method,
        url=url,
        headers=req_headers,
        params=query_params,
        json_body=json_body,
        form_body=form_body,
        timeout_seconds=timeout,
        retry_config=retry,
        request_id=request_id,
    )

    # 10. Format result as MCP content blocks
    return _format_result(response)


# ---------------------------------------------------------------------------
# MCP result formatting
# ---------------------------------------------------------------------------


def _format_result(response: HttpResponse) -> list[dict[str, Any]]:
    """Convert an HttpResponse to MCP content blocks."""
    blocks: list[dict[str, Any]] = []

    # Try to pretty-print JSON, fall back to raw text
    body = response.body
    try:
        parsed = json.loads(body)
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        text = body

    if response.truncated:
        text += "\n\n[Response truncated]"

    blocks.append({"type": "text", "text": text})
    return blocks


# ---------------------------------------------------------------------------
# Argument serialisation helpers
# ---------------------------------------------------------------------------


_MISSING = object()


def _serialize_path(value: Any, binding: ArgumentBinding) -> str:
    """Serialise a value for path substitution.

    Supports: simple (default), label, matrix.
    Arrays and objects use the explode flag.
    """
    style = binding.style or "simple"
    explode = binding.explode if binding.explode is not None else False

    if style == "simple":
        if isinstance(value, list):
            return ",".join(urllib.parse.quote(str(v), safe="") for v in value)
        if isinstance(value, dict):
            if explode:
                return ",".join(
                    f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
                    for k, v in value.items()
                )
            return ",".join(
                f"{urllib.parse.quote(str(k), safe='')},{urllib.parse.quote(str(v), safe='')}"
                for k, v in value.items()
            )
        return urllib.parse.quote(str(value), safe="")

    # label: .value or .k=v
    if style == "label":
        if isinstance(value, list):
            sep = "." if explode else ","
            return "." + sep.join(str(v) for v in value)
        if isinstance(value, dict):
            if explode:
                return "." + ".".join(f"{k}={v}" for k, v in value.items())
            return "." + ",".join(f"{k},{v}" for k, v in value.items())
        return f".{value}"

    # matrix: ;name=value
    if style == "matrix":
        name = binding.target_path[0]
        if isinstance(value, list):
            if explode:
                return "".join(f";{name}={v}" for v in value)
            return f";{name}={','.join(str(v) for v in value)}"
        if isinstance(value, dict):
            if explode:
                return "".join(f";{k}={v}" for k, v in value.items())
            return f";{name}={','.join(f'{k},{v}' for k, v in value.items())}"
        return f";{name}={value}"

    # Unknown style — fall back to simple
    return urllib.parse.quote(str(value), safe="")


def _serialize_query(
    llm_key: str, value: Any, binding: ArgumentBinding
) -> dict[str, str]:
    """Serialise a value for query string parameters.

    Returns a dict of {param_name: param_value} pairs (may be multi-valued
    when explode=True for arrays; httpx handles list values natively but we
    return flat strings for simplicity in v1).
    """
    style = binding.style or "form"
    explode = binding.explode if binding.explode is not None else True
    name = binding.target_path[0] if binding.target_path else llm_key

    if style == "form":
        if isinstance(value, list):
            if explode:
                # httpx accepts list values; return joined for flat dict
                return {name: ",".join(str(v) for v in value)}
            return {name: ",".join(str(v) for v in value)}
        if isinstance(value, dict):
            if explode:
                return {str(k): str(v) for k, v in value.items()}
            pairs = ",".join(f"{k},{v}" for k, v in value.items())
            return {name: pairs}
        return {name: str(value)}

    if style == "spaceDelimited":
        if isinstance(value, list):
            return {name: " ".join(str(v) for v in value)}
        return {name: str(value)}

    if style == "pipeDelimited":
        if isinstance(value, list):
            return {name: "|".join(str(v) for v in value)}
        return {name: str(value)}

    if style == "deepObject":
        # deepObject: name[key]=value for each key in an object
        if isinstance(value, dict):
            return {f"{name}[{k}]": str(v) for k, v in value.items()}
        return {name: str(value)}

    # Fallback
    return {name: str(value)}


def _serialize_simple(value: Any) -> str:
    """Serialise a scalar (or simple list) to a plain string for headers/cookies."""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k},{v}" for k, v in value.items())
    return str(value)


def _set_nested(obj: dict[str, Any], path: list[str], value: Any) -> None:
    """Set obj[path[0]][path[1]]...[path[-1]] = value, creating dicts as needed."""
    for key in path[:-1]:
        if key not in obj or not isinstance(obj[key], dict):
            obj[key] = {}
        obj = obj[key]
    obj[path[-1]] = value


def _pick_content_type(op: Operation) -> str | None:
    """Return the preferred content type for the request body."""
    if op.request_body is None or not op.request_body.variants:
        return None
    return op.request_body.variants[0].content_type


def _operation_may_stream(op: Operation) -> bool:
    """Return True if any declared 2xx response variant is text/event-stream.

    This is an optimisation: operations that never declare SSE skip the
    streaming path entirely. The actual runtime Content-Type from the
    upstream is the ground truth — stream_request() falls back to buffering
    if the server responds with a non-SSE content type.
    """
    for resp in op.responses:
        if resp.status_code.startswith("2"):
            for v in resp.variants:
                if "event-stream" in v.content_type:
                    return True
    return False


def _validate_args(
    tool_name: str,
    llm_args: dict[str, Any],
    input_schema: dict[str, Any],
    *,
    request_id: str | None,
) -> None:
    """Validate *llm_args* against *input_schema* (JSON Schema).

    Raises ArgumentValidationError if validation fails. This is a
    defence-in-depth check — the MCP SDK validates args before dispatch,
    but we re-validate here to catch schema drift and prevent malformed
    args from reaching the upstream API.
    """
    try:
        import jsonschema
        jsonschema.validate(instance=llm_args, schema=input_schema)
    except jsonschema.ValidationError as exc:
        from specmcp.errors import ArgumentValidationError
        raise ArgumentValidationError(
            f"Tool '{tool_name}' argument validation failed: {exc.message}",
            schema_path=".".join(str(p) for p in exc.absolute_path) or None,
            request_id=request_id,
        ) from exc
    except ImportError:
        # jsonschema not installed — skip validation silently.
        # This prevents a missing optional dep from breaking dispatch.
        pass
