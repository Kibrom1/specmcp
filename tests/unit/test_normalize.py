"""Unit tests for the Normalize stage."""

from __future__ import annotations

from typing import Any

import pytest

from specmcp.core.load import ResolvedSpec
from specmcp.core.normalize import (
    _normalize_schema_30_to_31,
    normalize,
)
from specmcp.errors import NormalizeError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolved(data: dict[str, Any], version: str = "3.0") -> ResolvedSpec:
    return ResolvedSpec(data=data, source="test.yaml", openapi_version=version)


def _petstore_spec() -> dict[str, Any]:
    """Minimal Petstore spec from the design doc §5.1."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Petstore", "version": "1.0.0"},
        "servers": [{"url": "https://petstore.example.com/v1"}],
        "paths": {
            "/pets/{petId}": {
                "get": {
                    "operationId": "getPetById",
                    "summary": "Get a pet by ID",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "format": "int64"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "A pet",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Pet"
                                    }
                                }
                            },
                        },
                        "404": {"description": "Not found"},
                    },
                    "security": [{"petstoreApiKey": []}],
                }
            }
        },
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "required": ["id", "name"],
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "name": {"type": "string"},
                        "tag": {"type": "string", "nullable": True},
                    },
                }
            },
            "securitySchemes": {
                "petstoreApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
        },
    }


# ---------------------------------------------------------------------------
# Schema normalization 3.0 → 3.1
# ---------------------------------------------------------------------------


def test_nullable_becomes_type_list():
    schema = {"type": "string", "nullable": True}
    result = _normalize_schema_30_to_31(schema)
    assert result["type"] == ["string", "null"]
    assert "nullable" not in result


def test_nullable_on_untyped_schema():
    schema = {"nullable": True}
    result = _normalize_schema_30_to_31(schema)
    assert "null" in result["type"]


def test_nullable_false_unchanged():
    schema = {"type": "string", "nullable": False}
    result = _normalize_schema_30_to_31(schema)
    assert result["type"] == "string"


def test_exclusive_maximum_boolean():
    schema = {"type": "integer", "maximum": 10, "exclusiveMaximum": True}
    result = _normalize_schema_30_to_31(schema)
    assert result["exclusiveMaximum"] == 10
    assert "maximum" not in result


def test_exclusive_minimum_boolean():
    schema = {"type": "integer", "minimum": 0, "exclusiveMinimum": True}
    result = _normalize_schema_30_to_31(schema)
    assert result["exclusiveMinimum"] == 0
    assert "minimum" not in result


def test_nested_nullable_in_properties():
    schema = {
        "type": "object",
        "properties": {
            "tag": {"type": "string", "nullable": True},
        },
    }
    result = _normalize_schema_30_to_31(schema)
    assert result["properties"]["tag"]["type"] == ["string", "null"]


def test_nullable_in_array_items():
    schema = {"type": "array", "items": {"type": "string", "nullable": True}}
    result = _normalize_schema_30_to_31(schema)
    assert "null" in result["items"]["type"]


def test_no_double_null():
    schema = {"type": ["string", "null"], "nullable": True}
    result = _normalize_schema_30_to_31(schema)
    assert result["type"].count("null") == 1


# ---------------------------------------------------------------------------
# Normalize — Petstore canonical example (from design doc §5.3)
# ---------------------------------------------------------------------------


def test_normalize_petstore_produces_operation():
    spec = _petstore_spec()
    # Inline the $ref for testing (in production prance does this)
    pet_schema = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "name": {"type": "string"},
            "tag": {"type": "string", "nullable": True},
        },
    }
    spec["paths"]["/pets/{petId}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = pet_schema

    ops = normalize(_resolved(spec))
    assert len(ops) == 1
    op = ops[0]

    assert op.id == "getPetById"
    assert op.method == "GET"
    assert op.path == "/pets/{petId}"
    assert op.server_url == "https://petstore.example.com/v1"
    assert op.summary == "Get a pet by ID"


def test_normalize_petstore_parameters():
    spec = _petstore_spec()
    ops = normalize(_resolved(spec))
    op = ops[0]

    assert len(op.parameters) == 2
    pet_id = next(p for p in op.parameters if p.name == "petId")
    verbose = next(p for p in op.parameters if p.name == "verbose")

    assert pet_id.location == "path"
    assert pet_id.required is True
    assert pet_id.style == "simple"
    assert pet_id.explode is False

    assert verbose.location == "query"
    assert verbose.required is False
    assert verbose.style == "form"
    assert verbose.explode is True


def test_normalize_petstore_nullable_tag():
    """The 'tag' field must be normalized from nullable:true to type:["string","null"]."""
    spec = _petstore_spec()
    # Inline pet schema
    spec["paths"]["/pets/{petId}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = spec["components"]["schemas"]["Pet"]

    ops = normalize(_resolved(spec))
    op = ops[0]
    response_schema = op.responses[0].variants[0].schema_
    assert response_schema is not None
    tag_type = response_schema["properties"]["tag"]["type"]
    assert "null" in tag_type


def test_normalize_petstore_auth():
    spec = _petstore_spec()
    ops = normalize(_resolved(spec))
    op = ops[0]
    assert len(op.auth) == 1
    assert op.auth[0][0].scheme_name == "petstoreApiKey"


def test_normalize_petstore_responses_order():
    """200 should come before 404."""
    spec = _petstore_spec()
    ops = normalize(_resolved(spec))
    op = ops[0]
    assert op.responses[0].status_code == "200"
    assert op.responses[1].status_code == "404"


# ---------------------------------------------------------------------------
# Normalize — operation ID derivation
# ---------------------------------------------------------------------------


def test_operation_id_from_spec():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/users": {"get": {"operationId": "listUsers", "responses": {"200": {"description": "ok"}}}}
        },
    }
    ops = normalize(_resolved(spec))
    assert ops[0].id == "listUsers"


def test_operation_id_derived_when_missing():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/users": {"get": {"responses": {"200": {"description": "ok"}}}}
        },
    }
    ops = normalize(_resolved(spec))
    assert ops[0].id == "get_users"


def test_operation_id_collision_resolution():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/a": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"operationId": "get_a", "responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec))
    ids = [op.id for op in ops]
    # Both want "get_a" — first keeps it, second gets _2
    assert ids[0] == "get_a"
    assert ids[1] == "get_a_2"


# ---------------------------------------------------------------------------
# Normalize — reserved header stripping
# ---------------------------------------------------------------------------


def test_reserved_headers_are_stripped():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/items": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                    "parameters": [
                        {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                        {"name": "Content-Type", "in": "header", "schema": {"type": "string"}},
                        {"name": "X-Custom-Header", "in": "header", "schema": {"type": "string"}},
                    ],
                }
            }
        },
    }
    ops = normalize(_resolved(spec))
    param_names = [p.name for p in ops[0].parameters]
    assert "Authorization" not in param_names
    assert "Content-Type" not in param_names
    assert "X-Custom-Header" in param_names  # non-reserved is kept


# ---------------------------------------------------------------------------
# Normalize — path-level parameter merging
# ---------------------------------------------------------------------------


def test_path_level_params_merged():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/users/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "get": {
                    "responses": {"200": {"description": "ok"}},
                    "parameters": [
                        {"name": "format", "in": "query", "schema": {"type": "string"}}
                    ],
                },
            }
        },
    }
    ops = normalize(_resolved(spec))
    names = {p.name for p in ops[0].parameters}
    assert "id" in names
    assert "format" in names


def test_operation_level_param_overrides_path_level():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "get": {
                    "responses": {"200": {"description": "ok"}},
                    "parameters": [
                        # Same (name, in) — operation-level wins
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                    ],
                },
            }
        },
    }
    ops = normalize(_resolved(spec))
    id_param = next(p for p in ops[0].parameters if p.name == "id")
    assert id_param.schema_["type"] == "integer"  # operation-level wins


# ---------------------------------------------------------------------------
# Normalize — filters
# ---------------------------------------------------------------------------


def test_exclude_deprecated():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/old": {"get": {"deprecated": True, "responses": {"200": {"description": "ok"}}}},
            "/new": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec), include_deprecated=False)
    assert all(op.path == "/new" for op in ops)


def test_include_deprecated_by_default():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/old": {"get": {"deprecated": True, "responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec))
    assert len(ops) == 1
    assert ops[0].deprecated is True


def test_include_tags_filter():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/a": {"get": {"tags": ["pets"], "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"tags": ["users"], "responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec), include_tags=["pets"])
    assert len(ops) == 1
    assert ops[0].path == "/a"


def test_exclude_tags_filter():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/a": {"get": {"tags": ["internal"], "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"tags": ["public"], "responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec), exclude_tags=["internal"])
    assert len(ops) == 1
    assert ops[0].path == "/b"


def test_exclude_operations_filter():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/a": {"get": {"operationId": "getA", "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"operationId": "getB", "responses": {"200": {"description": "ok"}}}},
        },
    }
    ops = normalize(_resolved(spec), exclude_operations=["getA"])
    assert len(ops) == 1
    assert ops[0].id == "getB"


# ---------------------------------------------------------------------------
# Normalize — server URL
# ---------------------------------------------------------------------------


def test_server_url_from_spec():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/v2"}],
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    ops = normalize(_resolved(spec))
    assert ops[0].server_url == "https://api.example.com/v2"


def test_base_url_override():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    ops = normalize(_resolved(spec), base_url_override="https://staging.example.com/v1")
    assert ops[0].server_url == "https://staging.example.com/v1"


# ---------------------------------------------------------------------------
# Normalize — request body
# ---------------------------------------------------------------------------


def test_request_body_normalized():
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/items": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }
    ops = normalize(_resolved(spec))
    rb = ops[0].request_body
    assert rb is not None
    assert rb.required is True
    assert rb.variants[0].content_type == "application/json"
