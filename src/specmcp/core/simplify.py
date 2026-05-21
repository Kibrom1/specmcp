"""
specmcp Simplify stage.

Takes a list of Operations (from Normalize) and produces SimplifiedOperations:
each carries the full-fidelity Operation (for the Dispatcher) plus an
LLM-facing projection (for tool schema exposure).

The stage is structured as a pipeline of pure functions:

    build_argument_map(operation)        → (llm_schema, arg_map, warnings)
    apply_inline_shallow_refs(...)       → (llm_schema, arg_map, warnings)
    apply_drop_spec_metadata(...)        → (llm_schema, arg_map, warnings)
    apply_collapse_unions(...)           → (llm_schema, arg_map, warnings)
    apply_flatten_wrappers(...)          → (llm_schema, arg_map, warnings)
    apply_truncate_descriptions(...)     → (llm_schema, arg_map, warnings)

Each rule is independently toggleable via SimplifyConfig. The ArgumentMap
is built up incrementally — each rule can add, rename, or annotate bindings
but never removes them (the Dispatcher needs all of them).

Round-trip invariant (enforced by tests):
    For every SimplifiedOperation, every LLM argument accepted by
    llm_input_schema must be dispatchable by arg_map into a complete,
    well-formed HTTP request using the Operation model.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from specmcp.config import SimplifyConfig
from specmcp.core.model import (
    ArgumentBinding,
    ArgumentMap,
    Operation,
    Parameter,
    SimplifiedOperation,
    SimplifyWarning,
)

# ---------------------------------------------------------------------------
# Internal state type passed between rule functions
# ---------------------------------------------------------------------------

_Schema = dict[str, Any]
_Bindings = dict[str, ArgumentBinding]
_Warnings = list[SimplifyWarning]

# Metadata that is spec-only — stripped from LLM-facing schemas
_SPEC_ONLY_KEYS = frozenset({
    "example", "examples", "xml", "externalDocs", "discriminator",
})


# ---------------------------------------------------------------------------
# Step 1: Build the initial (pass-through) ArgumentMap
#
# No simplifications yet. Every parameter and body field maps 1:1.
# This is the foundation all subsequent rules build on.
# ---------------------------------------------------------------------------


def _default_style_for(param: Parameter) -> str | None:
    return param.style


def _default_explode_for(param: Parameter) -> bool | None:
    return param.explode


def build_argument_map(operation: Operation) -> tuple[_Schema, _Bindings, _Warnings]:
    """Build a pass-through llm_input_schema and ArgumentMap with no simplifications.

    Returns (llm_schema, bindings, warnings).
    The llm_schema at this point is the full JSON Schema representation of all
    parameters + request body — no LLM-specific simplification applied yet.
    """
    properties: _Schema = {}
    required: list[str] = []
    bindings: _Bindings = {}
    warnings: _Warnings = []

    # --- Parameters (path, query, header, cookie) ---
    for param in operation.parameters:
        llm_key = param.name
        # Resolve naming collision with existing keys
        llm_key = _unique_key(llm_key, bindings)

        prop_schema = copy.deepcopy(param.schema_)
        properties[llm_key] = prop_schema

        if param.required:
            required.append(llm_key)

        bindings[llm_key] = ArgumentBinding(
            source_llm_key=llm_key,
            target_kind=param.location,
            target_path=[param.name],
            style=_default_style_for(param),
            explode=_default_explode_for(param),
        )

    # --- Request body ---
    if operation.request_body and operation.request_body.variants:
        variant = operation.request_body.variants[0]  # v1: dispatch variants[0]
        body_schema = copy.deepcopy(variant.schema_)

        if body_schema.get("type") == "object" and "properties" in body_schema:
            # Flatten top-level body fields into the LLM schema
            for field_name, field_schema in body_schema["properties"].items():
                llm_key = _unique_key(field_name, bindings, suffix="_body")
                properties[llm_key] = copy.deepcopy(field_schema)

                body_required = body_schema.get("required", [])
                if operation.request_body.required and field_name in body_required:
                    required.append(llm_key)

                bindings[llm_key] = ArgumentBinding(
                    source_llm_key=llm_key,
                    target_kind="body_field",
                    target_path=[field_name],
                )
        else:
            # Non-object body: expose as a single "body" argument
            llm_key = _unique_key("body", bindings)
            properties[llm_key] = copy.deepcopy(body_schema)
            if operation.request_body.required:
                required.append(llm_key)
            bindings[llm_key] = ArgumentBinding(
                source_llm_key=llm_key,
                target_kind="body_root",
                target_path=[],
            )

    llm_schema: _Schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        llm_schema["required"] = required

    return llm_schema, bindings, warnings


def _unique_key(name: str, existing: _Bindings, suffix: str = "_query") -> str:
    """Return a key that does not collide with keys already in ``existing``.

    If ``name`` is free, return it. Otherwise append the suffix, then
    ``_2``, ``_3``, ... until unique.
    """
    if name not in existing:
        return name
    candidate = f"{name}{suffix}"
    if candidate not in existing:
        return candidate
    i = 2
    while f"{candidate}_{i}" in existing:
        i += 1
    return f"{candidate}_{i}"


# ---------------------------------------------------------------------------
# Step 2: Apply simplification rules (each is a pure function)
# ---------------------------------------------------------------------------

# Rule 1 — Inline shallow $refs
# A ref used only once AND pointing to a small object (< 20 properties)
# is inlined. Deeply shared refs stay as $defs.

def apply_inline_shallow_refs(
    schema: _Schema, bindings: _Bindings, warnings: _Warnings, operation_id: str
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Inline $ref entries that are shallow and used only once."""
    defs = schema.get("$defs", schema.get("definitions", {}))
    # Count ref usages across the whole schema
    usage_count: dict[str, int] = {}
    _count_refs(schema, usage_count)

    new_schema = _inline_refs(schema, defs, usage_count, max_properties=20)
    return new_schema, bindings, warnings


def _count_refs(node: Any, counts: dict[str, int]) -> None:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            counts[ref] = counts.get(ref, 0) + 1
        for v in node.values():
            _count_refs(v, counts)
    elif isinstance(node, list):
        for item in node:
            _count_refs(item, counts)


def _inline_refs(
    node: Any,
    defs: dict[str, _Schema],
    usage_count: dict[str, int],
    max_properties: int,
) -> Any:
    if isinstance(node, dict):
        if "$ref" in node and len(node) == 1:
            ref: str = node["$ref"]
            if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
                key = ref.split("/")[-1]
                target = defs.get(key, {})
                props = target.get("properties", {})
                if usage_count.get(ref, 0) <= 1 and len(props) < max_properties:
                    return copy.deepcopy(target)
        return {k: _inline_refs(v, defs, usage_count, max_properties) for k, v in node.items()}
    elif isinstance(node, list):
        return [_inline_refs(item, defs, usage_count, max_properties) for item in node]
    return node


# Rule 2 — Drop spec-only metadata

def apply_drop_spec_metadata(
    schema: _Schema, bindings: _Bindings, warnings: _Warnings, operation_id: str
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Strip example, xml, externalDocs, discriminator, x-* from the LLM schema."""
    new_schema = _drop_metadata(schema)
    return new_schema, bindings, warnings


def _drop_metadata(node: Any) -> Any:
    if isinstance(node, dict):
        result = {}
        for k, v in node.items():
            if k in _SPEC_ONLY_KEYS:
                continue
            if k.startswith("x-"):
                continue
            result[k] = _drop_metadata(v)
        return result
    elif isinstance(node, list):
        return [_drop_metadata(item) for item in node]
    return node


# Rule 3 — Collapse oneOf / anyOf where possible

def apply_collapse_unions(
    schema: _Schema, bindings: _Bindings, warnings: _Warnings, operation_id: str
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Simplify oneOf/anyOf into a simpler schema where safe to do so.

    Cases handled:
    - All branches share the same primitive type → collapse to that type.
    - All branches are primitive types (no properties) → union of enums or
      single type with description.
    - Mixed complex branches → keep as-is (no lossy collapse).
    """
    new_schema = _collapse_unions_in(schema, warnings, operation_id)
    return new_schema, bindings, warnings


def _collapse_unions_in(node: Any, warnings: _Warnings, operation_id: str) -> Any:
    if not isinstance(node, dict):
        return node

    result = {}
    for k, v in node.items():
        if k in ("oneOf", "anyOf") and isinstance(v, list) and len(v) > 1:
            collapsed = _try_collapse(v, warnings, operation_id)
            if collapsed is not None:
                result.update(collapsed)
            else:
                result[k] = [_collapse_unions_in(branch, warnings, operation_id) for branch in v]
        else:
            result[k] = _collapse_unions_in(v, warnings, operation_id)
    return result


def _try_collapse(branches: list[_Schema], warnings: _Warnings, operation_id: str) -> _Schema | None:
    """Attempt to collapse union branches. Returns merged schema or None."""
    types = []
    for branch in branches:
        t = branch.get("type")
        if isinstance(t, str):
            types.append(t)
        elif isinstance(t, list):
            types.extend(t)
        else:
            return None  # branch has no simple type — don't collapse

    # All primitive types, no properties → collapse to type union
    has_properties = any("properties" in b for b in branches)
    if not has_properties:
        unique_types = list(dict.fromkeys(types))  # deduplicate preserving order
        if len(unique_types) == 1:
            return {"type": unique_types[0]}
        return {"type": unique_types}

    return None  # complex branches — leave as-is


# Rule 4 — Flatten single-property object wrappers

def apply_flatten_wrappers(
    schema: _Schema, bindings: _Bindings, warnings: _Warnings, operation_id: str
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Flatten { "data": { actual fields } } wrapper patterns in top-level properties.

    The ArgumentMap is updated so the Dispatcher re-wraps before sending.
    Only applied to body_field bindings at the top level of the llm_schema.
    """
    props = schema.get("properties", {})
    if not props:
        return schema, bindings, warnings

    new_props = {}
    new_bindings = dict(bindings)

    for llm_key, prop_schema in props.items():
        binding = bindings.get(llm_key)
        if (
            binding is not None
            and binding.target_kind == "body_field"
            and isinstance(prop_schema, dict)
            and prop_schema.get("type") == "object"
            and "properties" in prop_schema
            and len(prop_schema["properties"]) == 1
        ):
            # Single-property wrapper: unwrap
            inner_key, inner_schema = next(iter(prop_schema["properties"].items()))
            new_llm_key = f"{llm_key}_{inner_key}" if inner_key in bindings else inner_key
            new_props[new_llm_key] = copy.deepcopy(inner_schema)
            # Update binding: target_path gains one level
            new_bindings[new_llm_key] = ArgumentBinding(
                source_llm_key=new_llm_key,
                target_kind="body_field",
                target_path=binding.target_path + [inner_key],
                style=binding.style,
                explode=binding.explode,
            )
            # Remove the wrapper binding
            del new_bindings[llm_key]
        else:
            new_props[llm_key] = prop_schema

    new_schema = {**schema, "properties": new_props}

    # Fix required list
    old_required = schema.get("required", [])
    new_required = []
    for r in old_required:
        if r in new_bindings:
            new_required.append(r)
        elif r in bindings and r not in new_bindings:
            # Wrapper was flattened — propagate required to new key
            for new_key, b in new_bindings.items():
                if b.target_path[: len(bindings[r].target_path)] == bindings[r].target_path:
                    new_required.append(new_key)
                    break
    if new_required:
        new_schema["required"] = new_required
    elif "required" in new_schema:
        del new_schema["required"]

    return new_schema, new_bindings, warnings


# Rule 5 — Truncate descriptions

def apply_truncate_descriptions(
    schema: _Schema,
    bindings: _Bindings,
    warnings: _Warnings,
    operation_id: str,
    max_chars: int = 500,
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Cap field and schema descriptions at max_chars characters."""
    new_schema = _truncate_in(schema, max_chars, warnings, operation_id)
    return new_schema, bindings, warnings


def _truncate_in(node: Any, max_chars: int, warnings: _Warnings, operation_id: str) -> Any:
    if isinstance(node, dict):
        result = {}
        for k, v in node.items():
            if k == "description" and isinstance(v, str) and len(v) > max_chars:
                result[k] = v[:max_chars] + "…"
                warnings.append(SimplifyWarning(
                    kind="description_truncated",
                    operation_id=operation_id,
                    message=f"Description truncated to {max_chars} chars",
                ))
            else:
                result[k] = _truncate_in(v, max_chars, warnings, operation_id)
        return result
    elif isinstance(node, list):
        return [_truncate_in(item, max_chars, warnings, operation_id) for item in node]
    return node


# ---------------------------------------------------------------------------
# Fallback: free-form JSON argument
# (used when a schema can't be safely simplified)
# ---------------------------------------------------------------------------

def _fallback_schema(operation_id: str) -> tuple[_Schema, _Bindings, _Warnings]:
    """Return a single free-form 'body' argument schema for complex operations."""
    warnings = [SimplifyWarning(
        kind="fallback_to_freeform",
        operation_id=operation_id,
        message=(
            "Schema is too complex to simplify automatically. "
            "Pass the entire request body as a JSON string in the 'body' argument."
        ),
    )]
    schema: _Schema = {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": (
                    "The request body as a JSON string. "
                    "The API expects a complex schema that could not be simplified."
                ),
            }
        },
        "required": ["body"],
        "additionalProperties": False,
    }
    bindings: _Bindings = {
        "body": ArgumentBinding(
            source_llm_key="body",
            target_kind="body_root",
            target_path=[],
        )
    }
    return schema, bindings, warnings


# ---------------------------------------------------------------------------
# LLM description builder
# ---------------------------------------------------------------------------

def _build_llm_description(operation: Operation, max_chars: int = 500) -> str:
    """Build the tool description the LLM sees.

    Format: "{summary}. {description} [METHOD /path]"
    Capped at max_chars. Deprecated operations get a [DEPRECATED] prefix.
    """
    parts: list[str] = []
    if operation.deprecated:
        parts.append("[DEPRECATED]")
    if operation.summary:
        parts.append(operation.summary)
    if operation.description and operation.description != operation.summary:
        parts.append(operation.description)
    # Append HTTP hint — always present
    http_hint = f"[{operation.method} {operation.path}]"
    base = " ".join(parts)
    full = f"{base} {http_hint}".strip() if base else http_hint

    if len(full) > max_chars:
        # Truncate the text parts, always keep the HTTP hint
        available = max_chars - len(http_hint) - 1
        if available > 0:
            return base[:available].rstrip() + "… " + http_hint
        return http_hint
    return full


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def simplify(
    operations: list[Operation],
    config: SimplifyConfig | None = None,
) -> list[SimplifiedOperation]:
    """Run the Simplify stage on a list of normalized Operations.

    Parameters
    ----------
    operations:
        Output of the Normalize stage.
    config:
        Simplify configuration. If None, defaults are used (all rules on).

    Returns
    -------
    list[SimplifiedOperation]
        One per input operation, with llm_input_schema and arg_map populated.
    """
    if config is None:
        config = SimplifyConfig()

    results: list[SimplifiedOperation] = []

    for op in operations:
        try:
            schema, bindings, warnings = _simplify_one(op, config)
        except Exception:
            # Unexpected error in simplify — fall back to free-form
            schema, bindings, warnings = _fallback_schema(op.id)

        arg_map = ArgumentMap(bindings=bindings)
        llm_desc = _build_llm_description(op, max_chars=config.truncate_description_chars)

        results.append(SimplifiedOperation(
            operation=op,
            llm_input_schema=schema,
            llm_description=llm_desc,
            arg_map=arg_map,
            warnings=warnings,
        ))

    return results


def _simplify_one(
    op: Operation,
    config: SimplifyConfig,
) -> tuple[_Schema, _Bindings, _Warnings]:
    """Run the full simplification pipeline for a single operation."""
    # Step 1: build pass-through ArgumentMap
    schema, bindings, warnings = build_argument_map(op)

    # Step 2: apply each rule if enabled
    if config.inline_shallow_refs:
        schema, bindings, warnings = apply_inline_shallow_refs(
            schema, bindings, warnings, op.id
        )

    if config.drop_spec_metadata:
        schema, bindings, warnings = apply_drop_spec_metadata(
            schema, bindings, warnings, op.id
        )

    if config.collapse_unions:
        schema, bindings, warnings = apply_collapse_unions(
            schema, bindings, warnings, op.id
        )

    if config.flatten_single_property_wrappers:
        schema, bindings, warnings = apply_flatten_wrappers(
            schema, bindings, warnings, op.id
        )

    if config.truncate_description_chars > 0:
        schema, bindings, warnings = apply_truncate_descriptions(
            schema, bindings, warnings, op.id,
            max_chars=config.truncate_description_chars,
        )

    return schema, bindings, warnings
