"""Unit tests for the Simplify stage."""

from __future__ import annotations

from typing import Any

import pytest

from specmcp.config import SimplifyConfig
from specmcp.core.model import (
    AuthRequirement,
    Operation,
    Parameter,
    RequestBody,
    RequestBodyVariant,
    Response,
    ResponseVariant,
    SimplifiedOperation,
)
from specmcp.core.simplify import (
    _build_llm_description,
    _fallback_schema,
    apply_collapse_unions,
    apply_drop_spec_metadata,
    apply_flatten_wrappers,
    apply_inline_shallow_refs,
    apply_truncate_descriptions,
    build_argument_map,
    simplify,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _petstore_op() -> Operation:
    """Canonical getPetById from design doc §5.3."""
    return Operation(
        id="getPetById",
        method="GET",
        path="/pets/{petId}",
        server_url="https://petstore.example.com/v1",
        parameters=[
            Parameter(
                name="petId",
                location="path",
                required=True,
                schema_={"type": "integer", "format": "int64"},
                style="simple",
                explode=False,
            ),
            Parameter(
                name="verbose",
                location="query",
                required=False,
                schema_={"type": "boolean", "default": False},
                style="form",
                explode=True,
            ),
        ],
        request_body=None,
        responses=[Response(status_code="200", description="A pet")],
        auth=[[AuthRequirement(scheme_name="petstoreApiKey", scopes=[])]],
        summary="Get a pet by ID",
    )


def _post_op_with_body() -> Operation:
    """POST operation with a JSON request body."""
    return Operation(
        id="createPet",
        method="POST",
        path="/pets",
        server_url="https://petstore.example.com/v1",
        parameters=[],
        request_body=RequestBody(
            required=True,
            variants=[
                RequestBodyVariant(
                    content_type="application/json",
                    schema_={
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "tag": {"type": "string"},
                        },
                    },
                )
            ],
        ),
        responses=[Response(status_code="201", description="Created")],
        auth=[],
        summary="Create a pet",
    )


# ---------------------------------------------------------------------------
# build_argument_map — pass-through
# ---------------------------------------------------------------------------


def test_build_argument_map_parameters_only():
    """Parameters map 1:1 to LLM schema properties."""
    op = _petstore_op()
    schema, bindings, warnings = build_argument_map(op)

    assert schema["type"] == "object"
    assert "petId" in schema["properties"]
    assert "verbose" in schema["properties"]
    assert schema["required"] == ["petId"]
    assert warnings == []


def test_build_argument_map_path_binding():
    op = _petstore_op()
    _, bindings, _ = build_argument_map(op)

    pet_binding = bindings["petId"]
    assert pet_binding.target_kind == "path"
    assert pet_binding.target_path == ["petId"]
    assert pet_binding.style == "simple"
    assert pet_binding.explode is False


def test_build_argument_map_query_binding():
    op = _petstore_op()
    _, bindings, _ = build_argument_map(op)

    v_binding = bindings["verbose"]
    assert v_binding.target_kind == "query"
    assert v_binding.style == "form"
    assert v_binding.explode is True


def test_build_argument_map_body_flattened():
    """Object request body fields are flattened into the LLM schema."""
    op = _post_op_with_body()
    schema, bindings, warnings = build_argument_map(op)

    assert "name" in schema["properties"]
    assert "tag" in schema["properties"]
    assert "name" in schema["required"]

    name_binding = bindings["name"]
    assert name_binding.target_kind == "body_field"
    assert name_binding.target_path == ["name"]


def test_build_argument_map_name_collision_resolved():
    """If a path param and a body field share a name, the body field gets a suffix."""
    op = Operation(
        id="updateUser",
        method="PUT",
        path="/users/{id}",
        server_url="https://api.example.com",
        parameters=[
            Parameter(
                name="id", location="path", required=True,
                schema_={"type": "string"},
            ),
        ],
        request_body=RequestBody(
            required=True,
            variants=[RequestBodyVariant(
                content_type="application/json",
                schema_={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                },
            )],
        ),
        responses=[],
        auth=[],
        summary="Update user",
    )
    schema, bindings, warnings = build_argument_map(op)
    keys = set(schema["properties"].keys())
    assert "id" in keys
    # Body 'id' must have got a disambiguating suffix
    assert len(keys) == 2  # path 'id' + body 'id_body'
    assert "id_body" in keys
    assert bindings["id"].target_kind == "path"
    assert bindings["id_body"].target_kind == "body_field"


def test_build_argument_map_non_object_body():
    """A non-object body schema becomes a single 'body' argument."""
    op = Operation(
        id="rawPost",
        method="POST",
        path="/raw",
        server_url="https://api.example.com",
        parameters=[],
        request_body=RequestBody(
            required=True,
            variants=[RequestBodyVariant(
                content_type="application/json",
                schema_={"type": "string"},
            )],
        ),
        responses=[],
        auth=[],
        summary="Raw post",
    )
    schema, bindings, warnings = build_argument_map(op)
    assert "body" in schema["properties"]
    assert bindings["body"].target_kind == "body_root"


# ---------------------------------------------------------------------------
# apply_drop_spec_metadata
# ---------------------------------------------------------------------------


def test_drop_spec_metadata_strips_example():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "example": "Fluffy", "description": "The name"},
        },
    }
    result, _, _ = apply_drop_spec_metadata(schema, {}, [], "op")
    assert "example" not in result["properties"]["name"]
    assert result["properties"]["name"]["description"] == "The name"


def test_drop_spec_metadata_strips_xml():
    schema = {"type": "string", "xml": {"name": "petName"}, "x-custom": "value"}
    result, _, _ = apply_drop_spec_metadata(schema, {}, [], "op")
    assert "xml" not in result
    assert "x-custom" not in result


def test_drop_spec_metadata_keeps_non_spec_keys():
    schema = {"type": "string", "description": "A value", "format": "date"}
    result, _, _ = apply_drop_spec_metadata(schema, {}, [], "op")
    assert result["type"] == "string"
    assert result["description"] == "A value"
    assert result["format"] == "date"


# ---------------------------------------------------------------------------
# apply_collapse_unions
# ---------------------------------------------------------------------------


def test_collapse_anyof_same_primitive_type():
    schema = {
        "type": "object",
        "properties": {
            "val": {"anyOf": [{"type": "string"}, {"type": "string"}]},
        },
    }
    result, _, _ = apply_collapse_unions(schema, {}, [], "op")
    assert result["properties"]["val"] == {"type": "string"}


def test_collapse_anyof_multiple_primitive_types():
    schema = {
        "type": "object",
        "properties": {
            "val": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        },
    }
    result, _, _ = apply_collapse_unions(schema, {}, [], "op")
    assert set(result["properties"]["val"]["type"]) == {"string", "integer"}


def test_collapse_unions_leaves_complex_branches():
    """oneOf with object branches must NOT be collapsed."""
    schema = {
        "oneOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    }
    result, _, _ = apply_collapse_unions(schema, {}, [], "op")
    assert "oneOf" in result  # left as-is


# ---------------------------------------------------------------------------
# apply_flatten_wrappers
# ---------------------------------------------------------------------------


def test_flatten_single_property_wrapper():
    from specmcp.core.model import ArgumentBinding
    schema = {
        "type": "object",
        "properties": {
            "data": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    bindings = {
        "data": ArgumentBinding(
            source_llm_key="data",
            target_kind="body_field",
            target_path=["data"],
        )
    }
    result_schema, result_bindings, _ = apply_flatten_wrappers(schema, bindings, [], "op")
    # 'data' should be gone; 'name' (or 'data_name') should be present
    assert "data" not in result_schema["properties"]
    new_key = next(iter(result_schema["properties"]))
    assert result_bindings[new_key].target_path == ["data", "name"]


def test_flatten_does_not_touch_multi_property_objects():
    from specmcp.core.model import ArgumentBinding
    schema = {
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
            }
        },
    }
    bindings = {
        "payload": ArgumentBinding(
            source_llm_key="payload",
            target_kind="body_field",
            target_path=["payload"],
        )
    }
    result_schema, result_bindings, _ = apply_flatten_wrappers(schema, bindings, [], "op")
    # Multi-property object should NOT be flattened
    assert "payload" in result_schema["properties"]


# ---------------------------------------------------------------------------
# apply_truncate_descriptions
# ---------------------------------------------------------------------------


def test_truncate_description_in_property():
    long_desc = "x" * 600
    schema = {"type": "object", "properties": {"field": {"type": "string", "description": long_desc}}}
    result, _, warnings = apply_truncate_descriptions(schema, {}, [], "op", max_chars=500)
    assert len(result["properties"]["field"]["description"]) <= 502  # 500 + "…"
    assert result["properties"]["field"]["description"].endswith("…")
    assert any(w.kind == "description_truncated" for w in warnings)


def test_short_description_not_truncated():
    schema = {"type": "string", "description": "Short desc"}
    result, _, warnings = apply_truncate_descriptions(schema, {}, [], "op", max_chars=500)
    assert result["description"] == "Short desc"
    assert warnings == []


# ---------------------------------------------------------------------------
# apply_inline_shallow_refs
# ---------------------------------------------------------------------------


def test_inline_shallow_ref():
    schema = {
        "type": "object",
        "properties": {
            "pet": {"$ref": "#/$defs/Pet"},
        },
        "$defs": {
            "Pet": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
            }
        },
    }
    result, _, _ = apply_inline_shallow_refs(schema, {}, [], "op")
    # Pet ref used once and small → should be inlined
    pet_prop = result["properties"]["pet"]
    assert "$ref" not in pet_prop
    assert pet_prop["type"] == "object"


# ---------------------------------------------------------------------------
# _build_llm_description
# ---------------------------------------------------------------------------


def test_build_llm_description_basic():
    op = _petstore_op()
    desc = _build_llm_description(op)
    assert "Get a pet by ID" in desc
    assert "[GET /pets/{petId}]" in desc


def test_build_llm_description_deprecated():
    op = _petstore_op()
    object.__setattr__(op, "deprecated", True)
    desc = _build_llm_description(op)
    assert "[DEPRECATED]" in desc


def test_build_llm_description_no_summary():
    op = Operation(
        id="bare",
        method="DELETE",
        path="/items/{id}",
        server_url="https://api.example.com",
        parameters=[],
        responses=[],
        auth=[],
        summary=None,
    )
    desc = _build_llm_description(op)
    assert "[DELETE /items/{id}]" in desc


def test_build_llm_description_truncation():
    op = _petstore_op()
    desc = _build_llm_description(op, max_chars=10)
    # Even when truncated, the HTTP hint must appear
    assert "[GET /pets/{petId}]" in desc


# ---------------------------------------------------------------------------
# _fallback_schema
# ---------------------------------------------------------------------------


def test_fallback_schema_structure():
    schema, bindings, warnings = _fallback_schema("complexOp")
    assert "body" in schema["properties"]
    assert schema["properties"]["body"]["type"] == "string"
    assert bindings["body"].target_kind == "body_root"
    assert any(w.kind == "fallback_to_freeform" for w in warnings)


# ---------------------------------------------------------------------------
# simplify() — full pipeline (design doc §5.4)
# ---------------------------------------------------------------------------


def test_simplify_petstore_matches_design_doc():
    """The simplify output for getPetById must match §5.4 of the design doc."""
    op = _petstore_op()
    results = simplify([op])
    assert len(results) == 1
    result = results[0]

    assert result.llm_description == "Get a pet by ID [GET /pets/{petId}]"
    assert result.warnings == []

    schema = result.llm_input_schema
    assert schema["type"] == "object"
    assert "petId" in schema["properties"]
    assert "verbose" in schema["properties"]
    assert schema["required"] == ["petId"]

    arg_map = result.arg_map
    assert arg_map.bindings["petId"].target_kind == "path"
    assert arg_map.bindings["verbose"].target_kind == "query"


def test_simplify_preserves_full_operation():
    """The full Operation must be unchanged inside SimplifiedOperation."""
    op = _petstore_op()
    result = simplify([op])[0]
    assert result.operation.id == "getPetById"
    assert result.operation.server_url == "https://petstore.example.com/v1"
    assert len(result.operation.parameters) == 2


def test_simplify_with_body_operation():
    op = _post_op_with_body()
    result = simplify([op])[0]

    schema = result.llm_input_schema
    assert "name" in schema["properties"]
    assert "name" in schema["required"]

    arg_map = result.arg_map
    assert arg_map.bindings["name"].target_kind == "body_field"


def test_simplify_config_disable_drop_metadata():
    """With drop_spec_metadata=False, example keys must remain."""
    op = Operation(
        id="op",
        method="GET",
        path="/x",
        server_url="https://api.example.com",
        parameters=[
            Parameter(
                name="q",
                location="query",
                required=False,
                schema_={"type": "string", "example": "hello"},
            )
        ],
        responses=[],
        auth=[],
        summary="Op",
    )
    cfg = SimplifyConfig(drop_spec_metadata=False)
    result = simplify([op], config=cfg)[0]
    prop = result.llm_input_schema["properties"]["q"]
    assert "example" in prop


def test_simplify_config_disable_all_rules():
    """All rules off → schema is the raw pass-through from build_argument_map."""
    op = _petstore_op()
    cfg = SimplifyConfig(
        inline_shallow_refs=False,
        drop_spec_metadata=False,
        collapse_unions=False,
        flatten_single_property_wrappers=False,
    )
    result = simplify([op], config=cfg)[0]
    # Should still have the correct bindings
    assert "petId" in result.arg_map.bindings
    assert "verbose" in result.arg_map.bindings


def test_simplify_round_trip_invariant():
    """Every binding in arg_map must be dispatchable from llm_input_schema.

    For each binding key k:
      - k must appear in llm_input_schema.properties
      - binding.target_path must be non-empty (except body_root)
      - binding.target_kind must be a valid kind
    """
    op = _post_op_with_body()
    result = simplify([op])[0]

    valid_kinds = {"path", "query", "header", "cookie", "body_field", "body_root"}
    schema_props = result.llm_input_schema.get("properties", {})

    for key, binding in result.arg_map.bindings.items():
        assert key in schema_props, f"binding key '{key}' not in llm_input_schema.properties"
        assert binding.target_kind in valid_kinds
        if binding.target_kind != "body_root":
            assert len(binding.target_path) > 0, f"binding '{key}' has empty target_path"
