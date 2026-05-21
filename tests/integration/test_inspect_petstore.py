"""
Integration test: specmcp inspect against the vendored Petstore spec.

The --json snapshot is the regression guard for the naming algorithm and
Simplify defaults. Any change to the snapshot output is a breaking change.

Uses syrupy for snapshot assertion. To update snapshots:
    pytest tests/integration/test_inspect_petstore.py --snapshot-update
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from syrupy import SnapshotAssertion
from typer.testing import CliRunner

from specmcp.cli.app import app

PETSTORE_SPEC = Path(__file__).parent.parent.parent / "test-corpus" / "petstore.json"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Structural assertions (always-run, no snapshot needed)
# ---------------------------------------------------------------------------


def test_inspect_exits_zero():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC)])
    assert r.exit_code == 0, f"inspect failed:\n{r.output}"


def test_inspect_json_exits_zero():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    assert r.exit_code == 0, f"inspect --json failed:\n{r.output}"


def test_inspect_json_is_valid():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    assert "tools" in data
    assert "tool_count" in data
    assert data["openapi_version"] == "3.0"


def test_inspect_petstore_tool_count():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    assert data["tool_count"] == 4  # listPets, createPet, getPetById, deletePet


def test_inspect_tool_names():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    names = [t["name"] for t in data["tools"]]
    assert "listPets" in names
    assert "createPet" in names
    assert "getPetById" in names
    assert "deletePet" in names


def test_inspect_get_pet_by_id_matches_design_doc():
    """getPetById must exactly match design doc §5.5."""
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    tool = next(t for t in data["tools"] if t["name"] == "getPetById")

    assert tool["name"] == "getPetById"
    assert tool["description"] == "Get a pet by ID [GET /pets/{petId}]"
    assert tool["method"] == "GET"
    assert tool["path"] == "/pets/{petId}"
    assert tool["auth_schemes"] == ["petstoreApiKey"]
    assert tool["deprecated"] is False

    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["petId"]
    assert "petId" in schema["properties"]
    assert schema["properties"]["petId"]["type"] == "integer"
    assert "verbose" in schema["properties"]
    assert schema["properties"]["verbose"]["type"] == "boolean"
    assert schema["properties"]["verbose"]["default"] is False


def test_inspect_no_warnings_for_clean_spec():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    assert data["warning_count"] == 0
    assert data["fallback_count"] == 0


def test_inspect_create_pet_body_flattened():
    """createPet body fields (name, tag) should appear as top-level LLM args."""
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    tool = next(t for t in data["tools"] if t["name"] == "createPet")

    props = tool["input_schema"]["properties"]
    assert "name" in props
    assert "tag" in props
    assert "name" in tool["input_schema"]["required"]


def test_inspect_human_output_contains_tool_names():
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC)])
    assert "getPetById" in r.output
    assert "listPets" in r.output
    assert "GET" in r.output


def test_inspect_spec_order_preserved():
    """Tools must appear in the order they appear in the spec."""
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    names = [t["name"] for t in data["tools"]]
    # Spec order: listPets (GET /pets), createPet (POST /pets),
    #             getPetById (GET /pets/{petId}), deletePet (DELETE /pets/{petId})
    assert names.index("listPets") < names.index("createPet")
    assert names.index("createPet") < names.index("getPetById")
    assert names.index("getPetById") < names.index("deletePet")


# ---------------------------------------------------------------------------
# Syrupy snapshot — regression guard for full JSON output
# ---------------------------------------------------------------------------


def test_inspect_json_snapshot(snapshot: SnapshotAssertion):
    """Full --json output snapshot. Any diff = potential breaking change."""
    r = runner.invoke(app, ["inspect", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    # Normalise: sort tool list by name for determinism; strip machine-specific
    # spec path so the snapshot is portable across environments.
    data["tools"] = sorted(data["tools"], key=lambda t: t["name"])
    data["spec"] = "test-corpus/petstore.json"
    assert json.dumps(data, indent=2, sort_keys=True) == snapshot
