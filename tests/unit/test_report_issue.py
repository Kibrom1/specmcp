"""Unit tests for specmcp report-issue command."""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specmcp.cli.app import app

runner = CliRunner()
PETSTORE_SPEC = Path(__file__).parent.parent.parent / "test-corpus" / "petstore.json"


def _parse_json(r: object) -> dict:
    """Parse the JSON report from the output.

    ``report-issue`` prints a JSON object followed by a human-readable hint
    line on stderr (which Typer's CliRunner merges into r.output). We extract
    just the JSON by finding the closing brace of the top-level object.
    """
    output: str = r.output  # type: ignore[attr-defined]
    # Find the end of the top-level JSON object
    depth = 0
    end = 0
    for i, ch in enumerate(output):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(output[:end])


def test_report_issue_help_exits_zero():
    r = runner.invoke(app, ["report-issue", "--help"])
    assert r.exit_code == 0


def test_report_issue_petstore_exits_zero():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    assert r.exit_code == 0, r.output


def test_report_issue_output_is_valid_json():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    data = _parse_json(r)
    assert "specmcp_version" in data
    assert "pipeline" in data
    assert "errors" in data


def test_report_issue_tool_count_correct():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    data = _parse_json(r)
    assert data["pipeline"]["tool_count"] == 4


def test_report_issue_no_errors_for_clean_spec():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    data = _parse_json(r)
    assert data["errors"] == []


def test_report_issue_includes_platform_info():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    data = _parse_json(r)
    assert "python_version" in data
    assert "platform" in data
    assert data["specmcp_version"] is not None


def test_report_issue_no_creds_in_output(monkeypatch, tmp_path):
    monkeypatch.setenv("PETSTORE_API_KEY", "SUPER_SECRET_CREDENTIAL")

    cfg_yaml = textwrap.dedent(f"""\
        version: "1"
        spec:
          source: {PETSTORE_SPEC}
        auth:
          petstoreApiKey:
            type: apiKey
            in: header
            name: X-Api-Key
            value_from: env(PETSTORE_API_KEY)
    """)
    cfg_path = tmp_path / "mcp.config.yaml"
    cfg_path.write_text(cfg_yaml)

    r = runner.invoke(app, [
        "report-issue",
        "--spec", str(PETSTORE_SPEC),
        "--config", str(cfg_path),
    ])
    assert r.exit_code == 0
    # The credential must not appear anywhere in the output
    assert "SUPER_SECRET_CREDENTIAL" not in r.output
    # But the env var *name* should appear (for debugging)
    assert "PETSTORE_API_KEY" in r.output


def test_report_issue_auth_summary_shows_env_var_name(monkeypatch, tmp_path):
    monkeypatch.setenv("PETSTORE_API_KEY", "secret")

    cfg_yaml = textwrap.dedent(f"""\
        version: "1"
        spec:
          source: {PETSTORE_SPEC}
        auth:
          petstoreApiKey:
            type: apiKey
            in: header
            name: X-Api-Key
            value_from: env(PETSTORE_API_KEY)
    """)
    cfg_path = tmp_path / "mcp.config.yaml"
    cfg_path.write_text(cfg_yaml)

    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC), "--config", str(cfg_path)])
    data = _parse_json(r)
    auth = data["config_summary"]["auth_schemes"]
    assert "petstoreApiKey" in auth
    assert auth["petstoreApiKey"]["value_from"] == "env(PETSTORE_API_KEY)"


def test_report_issue_write_to_file(tmp_path):
    out_file = tmp_path / "report.json"
    r = runner.invoke(app, [
        "report-issue",
        "--spec", str(PETSTORE_SPEC),
        "--output", str(out_file),
    ])
    assert r.exit_code == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["pipeline"]["tool_count"] == 4


def test_report_issue_bad_spec_records_error():
    r = runner.invoke(app, ["report-issue", "--spec", "/nonexistent/spec.json"])
    assert r.exit_code == 0  # report-issue always exits 0; errors go in the report
    data = _parse_json(r)
    assert len(data["errors"]) > 0
    assert any("load" in e.get("stage", "") for e in data["errors"])


def test_report_issue_includes_tool_names():
    r = runner.invoke(app, ["report-issue", "--spec", str(PETSTORE_SPEC)])
    data = _parse_json(r)
    tool_names = [t["name"] for t in data["pipeline"]["tools"]]
    assert "listPets" in tool_names
    assert "getPetById" in tool_names
