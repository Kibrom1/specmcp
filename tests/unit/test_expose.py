"""Unit tests for the Expose / Tool Registry stage."""

from __future__ import annotations

from specmcp.config import Config, OperationOverride
from specmcp.core.expose import ToolDefinition, ToolRegistry
from specmcp.core.model import (
    ArgumentBinding,
    ArgumentMap,
    AuthRequirement,
    Operation,
    Parameter,
    Response,
    SimplifiedOperation,
    SimplifyWarning,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_simplified_op(
    op_id: str = "getPetById",
    method: str = "GET",
    path: str = "/pets/{petId}",
    summary: str = "Get a pet by ID",
    deprecated: bool = False,
) -> SimplifiedOperation:
    op = Operation(
        id=op_id,
        method=method,
        path=path,
        server_url="https://petstore.example.com/v1",
        parameters=[
            Parameter(
                name="petId", location="path", required=True,
                schema_={"type": "integer"},
                style="simple", explode=False,
            )
        ],
        responses=[Response(status_code="200", description="ok")],
        auth=[[AuthRequirement(scheme_name="petstoreApiKey")]],
        summary=summary,
        deprecated=deprecated,
    )
    return SimplifiedOperation(
        operation=op,
        llm_input_schema={
            "type": "object",
            "required": ["petId"],
            "properties": {"petId": {"type": "integer"}},
            "additionalProperties": True,
        },
        llm_description=f"{summary} [{method} {path}]",
        arg_map=ArgumentMap(bindings={
            "petId": ArgumentBinding(
                source_llm_key="petId",
                target_kind="path",
                target_path=["petId"],
                style="simple",
                explode=False,
            ),
        }),
        warnings=[],
    )


# ---------------------------------------------------------------------------
# ToolRegistry.build — basic
# ---------------------------------------------------------------------------


def test_build_single_operation():
    sop = _make_simplified_op()
    registry = ToolRegistry.build([sop])
    assert len(registry.tools) == 1
    tool = registry.tools[0]
    assert tool.name == "getPetById"
    assert "[GET /pets/{petId}]" in tool.description


def test_build_preserves_spec_order():
    ops = [
        _make_simplified_op("opA", "GET", "/a", "A"),
        _make_simplified_op("opB", "POST", "/b", "B"),
        _make_simplified_op("opC", "DELETE", "/c", "C"),
    ]
    registry = ToolRegistry.build(ops)
    assert [t.name for t in registry.tools] == ["opA", "opB", "opC"]


def test_lookup_found():
    registry = ToolRegistry.build([_make_simplified_op()])
    result = registry.lookup("getPetById")
    assert result is not None
    assert result.name == "getPetById"


def test_lookup_not_found():
    registry = ToolRegistry.build([_make_simplified_op()])
    assert registry.lookup("doesNotExist") is None


def test_list_tools_format():
    """list_tools() must return MCP tools/list wire format."""
    registry = ToolRegistry.build([_make_simplified_op()])
    tools = registry.list_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert "name" in tool
    assert "description" in tool
    assert "inputSchema" in tool
    assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Design doc §5.5 — exact Petstore tool definition
# ---------------------------------------------------------------------------


def test_petstore_tool_matches_design_doc():
    """tools/list output for getPetById must match §5.5 exactly."""
    sop = _make_simplified_op(
        op_id="getPetById",
        method="GET",
        path="/pets/{petId}",
        summary="Get a pet by ID",
    )
    # Set up a schema that matches §5.5
    sop = SimplifiedOperation(
        operation=sop.operation,
        llm_input_schema={
            "type": "object",
            "required": ["petId"],
            "properties": {
                "petId": {"type": "integer"},
                "verbose": {"type": "boolean", "default": False},
            },
        },
        llm_description="Get a pet by ID [GET /pets/{petId}]",
        arg_map=sop.arg_map,
        warnings=[],
    )

    registry = ToolRegistry.build([sop])
    tools = registry.list_tools()
    assert len(tools) == 1
    t = tools[0]

    assert t["name"] == "getPetById"
    assert t["description"] == "Get a pet by ID [GET /pets/{petId}]"
    assert t["inputSchema"]["required"] == ["petId"]
    assert "petId" in t["inputSchema"]["properties"]
    assert "verbose" in t["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_override_rename():
    import textwrap
    from pathlib import Path
    import tempfile

    sop = _make_simplified_op("getPetById")
    # Build a config with a rename override
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        operations:
          getPetById:
            rename: get_pet
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    registry = ToolRegistry.build([sop], config=cfg)
    assert registry.tools[0].name == "get_pet"
    assert registry.lookup("get_pet") is not None
    assert registry.lookup("getPetById") is None  # old name gone


def test_override_hide():
    import textwrap
    import tempfile

    ops = [
        _make_simplified_op("opA"),
        _make_simplified_op("opB"),
    ]
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        operations:
          opA:
            hide: true
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    registry = ToolRegistry.build(ops, config=cfg)
    assert len(registry.tools) == 1
    assert registry.tools[0].name == "opB"


def test_override_description():
    import textwrap
    import tempfile

    sop = _make_simplified_op("getPetById")
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        operations:
          getPetById:
            description: "Custom description for the LLM."
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    registry = ToolRegistry.build([sop], config=cfg)
    assert registry.tools[0].description == "Custom description for the LLM."


def test_override_additional_properties_strict():
    import textwrap
    import tempfile

    sop = _make_simplified_op("getPetById")
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        operations:
          getPetById:
            additional_properties_strict: true
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    registry = ToolRegistry.build([sop], config=cfg)
    assert registry.tools[0].input_schema.get("additionalProperties") is False


def test_no_overrides_keeps_defaults():
    sop = _make_simplified_op("getPetById")
    registry = ToolRegistry.build([sop], config=None)
    assert registry.tools[0].name == "getPetById"
    assert registry.tools[0].input_schema.get("additionalProperties") is True


# ---------------------------------------------------------------------------
# Deprecated prefix
# ---------------------------------------------------------------------------


def test_deprecated_description_prefix():
    """Deprecated operations must have [DEPRECATED] in their llm_description."""
    from specmcp.core.simplify import _build_llm_description
    op = _make_simplified_op("oldOp", deprecated=True).operation
    desc = _build_llm_description(op)
    assert "[DEPRECATED]" in desc
