"""Unit tests for specmcp validate command."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specmcp.cli.app import app

runner = CliRunner()
PETSTORE_SPEC = Path(__file__).parent.parent.parent / "test-corpus" / "petstore.json"


def test_validate_petstore_exits_zero():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC)])
    assert r.exit_code == 0, f"validate failed:\n{r.output}"


def test_validate_petstore_shows_spec_valid():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC)])
    assert "✓ Spec valid" in r.output


def test_validate_petstore_shows_tool_count():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC)])
    assert "Tools exposed" in r.output


def test_validate_petstore_shows_operation_count():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC)])
    assert "Operations found" in r.output


def test_validate_json_exits_zero():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC), "--json"])
    assert r.exit_code == 0, f"validate --json failed:\n{r.output}"


def test_validate_json_has_required_fields():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    assert data["status"] == "ok"
    assert "operation_count" in data
    assert "tool_count" in data
    assert "hidden_count" in data
    assert "fallback_count" in data
    assert "auth_schemes" in data
    assert isinstance(data["warnings"], list)


def test_validate_json_tool_count_matches_petstore():
    r = runner.invoke(app, ["validate", "--spec", str(PETSTORE_SPEC), "--json"])
    data = json.loads(r.output)
    assert data["tool_count"] == 4
    assert data["operation_count"] == 4
    assert data["hidden_count"] == 0


def test_validate_nonexistent_spec_exits_nonzero():
    r = runner.invoke(app, ["validate", "--spec", "/nonexistent/spec.yaml"])
    assert r.exit_code != 0


def test_validate_nonexistent_config_without_spec_exits_nonzero():
    r = runner.invoke(app, ["validate", "--config", "/nonexistent/mcp.config.yaml"])
    assert r.exit_code != 0


def test_validate_auth_scheme_shown_in_output(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "secret")
    cfg_yaml = textwrap.dedent(f"""\
        version: "1"
        spec:
          source: {PETSTORE_SPEC}
        auth:
          myKey:
            type: apiKey
            in: header
            name: X-Api-Key
            value_from: env(MY_API_KEY)
    """)
    cfg_path = tmp_path / "mcp.config.yaml"
    cfg_path.write_text(cfg_yaml)

    r = runner.invoke(app, ["validate", "--config", str(cfg_path)])
    assert r.exit_code == 0
    # Auth schemes from the spec's securitySchemes should appear
    assert "Auth schemes" in r.output or "petstoreApiKey" in r.output
