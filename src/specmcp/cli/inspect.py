"""
specmcp inspect — list the tools that would be exposed, with schemas.

Runs the full pipeline: Load → Normalize → Simplify → Expose.

Human-readable output:
  Summary: N operations, M tools, K fallbacks, J warnings
  Per-tool: name, method+path, description, inputSchema (pretty), warnings, auth

--json output:
  Stable JSON schema documented in docs/inspect-output-schema.json
  Safe to pipe into CI or other tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

from specmcp.cli.app import app
from specmcp.errors import SpecmcpError, exit_code_for


@app.command("inspect")
def inspect_cmd(
    spec: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--spec",
        "-s",
        help="Path or URL to the OpenAPI spec (overrides config).",
    ),
    config: Optional[Path] = typer.Option(  # noqa: UP007
        Path("mcp.config.yaml"),
        "--config",
        "-c",
        help="Path to the config file.",
        show_default=True,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit stable machine-readable JSON output.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show verbose detail including all simplify warnings.",
    ),
) -> None:
    """List tools that would be exposed. Runs the full pipeline without serving."""
    from specmcp.config import Config, SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    # --- Load config ---
    cfg: Optional[Config] = None  # noqa: UP007
    if config and config.exists():
        try:
            cfg = Config.load(config)
        except SpecmcpError as exc:
            _emit_error(exc, json_output)
            raise typer.Exit(exit_code_for(exc))

    spec_source: str
    if spec:
        spec_source = spec
    elif cfg:
        spec_source = cfg.spec.source
    else:
        typer.echo("Error: provide --spec <path-or-url> or a config file.", err=True)
        raise typer.Exit(64)

    # --- Load + Parse ---
    try:
        raw, resolved = load_spec(spec_source)
    except SpecmcpError as exc:
        _emit_error(exc, json_output)
        raise typer.Exit(exit_code_for(exc))

    # --- Normalize ---
    try:
        ops = normalize(
            resolved,
            base_url_override=cfg.server.base_url_override if cfg else None,
            include_deprecated=cfg.server.include_deprecated if cfg else True,
            include_tags=cfg.server.include_tags if cfg else None,
            exclude_tags=cfg.server.exclude_tags if cfg else None,
            include_operations=cfg.server.include_operations if cfg else None,
            exclude_operations=cfg.server.exclude_operations if cfg else None,
        )
    except SpecmcpError as exc:
        _emit_error(exc, json_output)
        raise typer.Exit(exit_code_for(exc))

    # --- Simplify ---
    simplify_cfg = cfg.simplify if cfg else SimplifyConfig()
    simplified_ops = simplify(ops, config=simplify_cfg)

    # --- Expose ---
    registry = ToolRegistry.build(simplified_ops, config=cfg)

    # --- Gather stats ---
    total_ops = len(ops)
    total_tools = len(registry.tools)
    hidden = total_ops - total_tools

    all_warnings = [w for sop in simplified_ops for w in sop.warnings]
    fallback_count = sum(
        1 for sop in simplified_ops
        if any(w.kind == "fallback_to_freeform" for w in sop.warnings)
    )
    warning_count = len(all_warnings)

    if json_output:
        _output_json(registry, simplified_ops, resolved.openapi_version, spec_source, all_warnings)
    else:
        _output_human(
            registry, simplified_ops, resolved.openapi_version, spec_source,
            total_ops, total_tools, hidden, fallback_count, warning_count, verbose,
        )


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _output_human(
    registry: Any,
    simplified_ops: list[Any],
    openapi_version: str,
    spec_source: str,
    total_ops: int,
    total_tools: int,
    hidden: int,
    fallback_count: int,
    warning_count: int,
    verbose: bool,
) -> None:
    typer.echo(f"Spec    : {spec_source}  (OpenAPI {openapi_version})")
    typer.echo(
        f"Summary : {total_ops} operations → {total_tools} tools"
        + (f"  ({hidden} hidden)" if hidden else "")
        + (f"  [{fallback_count} fallbacks]" if fallback_count else "")
        + (f"  [{warning_count} warnings]" if warning_count else "")
    )
    typer.echo("")

    for tool in registry.tools:
        sop = tool.simplified_operation
        op = sop.operation
        typer.echo(f"  ┌─ {tool.name}")
        typer.echo(f"  │  {op.method} {op.server_url}{op.path}")
        typer.echo(f"  │  {tool.description}")

        # Auth schemes
        if op.auth:
            schemes = ", ".join(
                r.scheme_name
                for group in op.auth
                for r in group
            )
            typer.echo(f"  │  auth: {schemes}")

        if op.deprecated:
            typer.echo("  │  ⚠  DEPRECATED")

        # Input schema (pretty, indented)
        schema_str = json.dumps(tool.input_schema, indent=4)
        for line in schema_str.splitlines():
            typer.echo(f"  │    {line}")

        # Warnings
        if sop.warnings and verbose:
            for w in sop.warnings:
                typer.echo(f"  │  ⚠  {w.kind}: {w.message}")

        typer.echo("  └─")
        typer.echo("")


# ---------------------------------------------------------------------------
# JSON output (stable schema — see docs/inspect-output-schema.json)
# ---------------------------------------------------------------------------


def _output_json(
    registry: Any,
    simplified_ops: list[Any],
    openapi_version: str,
    spec_source: str,
    all_warnings: list[Any],
) -> None:
    sop_by_id = {sop.operation.id: sop for sop in simplified_ops}

    tools_json = []
    for tool in registry.tools:
        sop = tool.simplified_operation
        op = sop.operation
        tools_json.append({
            "name": tool.name,
            "operation_id": op.id,
            "method": op.method,
            "path": op.path,
            "server_url": op.server_url,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "auth_schemes": [
                r.scheme_name
                for group in op.auth
                for r in group
            ],
            "deprecated": op.deprecated,
            "tags": op.tags,
            "warnings": [
                {"kind": w.kind, "message": w.message, "field_path": w.field_path}
                for w in sop.warnings
            ],
        })

    output = {
        "spec": spec_source,
        "openapi_version": openapi_version,
        "tool_count": len(registry.tools),
        "hidden_count": len(simplified_ops) - len(registry.tools),
        "fallback_count": sum(
            1 for sop in simplified_ops
            if any(w.kind == "fallback_to_freeform" for w in sop.warnings)
        ),
        "warning_count": len(all_warnings),
        "tools": tools_json,
    }
    typer.echo(json.dumps(output, indent=2))


def _emit_error(exc: SpecmcpError, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"status": "error", **exc.to_dict()}, indent=2), err=True)
    else:
        loc = f"[{exc.location}] " if exc.location else ""
        typer.echo(f"Error: {loc}{exc.message}", err=True)
        if exc.detail:
            typer.echo(f"  {exc.detail}", err=True)
