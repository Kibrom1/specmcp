"""
Nightly integration test: fetch the live Petstore spec and run the full pipeline.

Fetches https://petstore3.swagger.io/api/v3/openapi.json, runs
load → normalize → simplify → expose, and asserts structural invariants.

Run manually:
    pytest tests/integration/test_petstore_nightly.py -v

Skipped automatically unless the SPECMCP_NIGHTLY env var is set or the
test is invoked directly (not via the regular `pytest` invocation in CI).
"""

from __future__ import annotations

import os

import pytest

# Skip in normal CI unless explicitly opted in.
pytestmark = pytest.mark.skipif(
    os.getenv("SPECMCP_NIGHTLY") is None and os.getenv("CI") is None,
    reason="Nightly tests skipped locally unless SPECMCP_NIGHTLY=1 is set.",
)

PETSTORE_LIVE_URL = "https://petstore3.swagger.io/api/v3/openapi.json"


@pytest.fixture(scope="module")
def petstore_pipeline():
    """Run the full pipeline against the live Petstore spec once per module."""
    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    raw, resolved = load_spec(PETSTORE_LIVE_URL)
    ops = normalize(resolved)
    simplified_ops = simplify(ops, config=SimplifyConfig())
    registry = ToolRegistry.build(simplified_ops, config=None)
    return resolved, ops, simplified_ops, registry


def test_live_spec_loads(petstore_pipeline):
    """The live spec should load without error."""
    resolved, *_ = petstore_pipeline
    assert resolved.openapi_version.startswith("3.")


def test_live_spec_has_operations(petstore_pipeline):
    """Petstore should expose at least 10 operations."""
    _, ops, *_ = petstore_pipeline
    assert len(ops) >= 10, f"Expected ≥10 operations, got {len(ops)}"


def test_live_spec_has_tools(petstore_pipeline):
    """All operations should map to at least one tool."""
    _, ops, simplified_ops, registry = petstore_pipeline
    assert len(registry.tools) > 0
    assert len(registry.tools) <= len(ops)


def test_live_spec_tool_names_are_unique(petstore_pipeline):
    """Tool names must be unique."""
    *_, registry = petstore_pipeline
    names = [t.name for t in registry.tools]
    assert len(names) == len(set(names)), f"Duplicate tool names: {sorted(set(n for n in names if names.count(n) > 1))}"


def test_live_spec_tools_have_valid_schemas(petstore_pipeline):
    """Every tool's inputSchema must be a valid JSON Schema object."""
    *_, registry = petstore_pipeline
    for tool in registry.tools:
        schema = tool.input_schema
        assert isinstance(schema, dict), f"Tool {tool.name}: inputSchema is not a dict"
        assert schema.get("type") == "object", f"Tool {tool.name}: inputSchema type is not 'object'"
        assert "properties" in schema or schema.get("additionalProperties") is not False, (
            f"Tool {tool.name}: inputSchema missing 'properties'"
        )


def test_live_spec_no_pipeline_errors(petstore_pipeline):
    """The pipeline should complete with no hard errors (fallbacks are ok)."""
    _, ops, simplified_ops, registry = petstore_pipeline
    # Every normalized op should produce exactly one simplified op
    assert len(simplified_ops) == len(ops)
    # Every simplified op should have a name
    for sop in simplified_ops:
        assert sop.tool_name, f"SimplifiedOperation for {sop.operation.id!r} has no tool_name"


def test_live_spec_validate_cli(tmp_path):
    """specmcp validate --spec <live URL> should exit 0."""
    from typer.testing import CliRunner

    from specmcp.cli.app import app

    runner = CliRunner()
    r = runner.invoke(app, ["validate", "--spec", PETSTORE_LIVE_URL])
    assert r.exit_code == 0, f"validate exited {r.exit_code}:\n{r.output}"
    assert "✓ Spec valid" in r.output
    assert "Tools exposed" in r.output


def test_live_spec_inspect_json_cli(tmp_path):
    """specmcp inspect --json --spec <live URL> should produce parseable JSON."""
    import json

    from typer.testing import CliRunner

    from specmcp.cli.app import app

    runner = CliRunner()
    r = runner.invoke(app, ["inspect", "--spec", PETSTORE_LIVE_URL, "--json"])
    assert r.exit_code == 0, f"inspect --json exited {r.exit_code}:\n{r.output}"
    data = json.loads(r.output)
    assert data["tool_count"] > 0
    assert "hidden_count" in data
    assert isinstance(data["tools"], list)
