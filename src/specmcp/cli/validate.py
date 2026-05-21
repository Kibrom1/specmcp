"""
specmcp validate — validate the spec and config without serving.

Runs Load + Normalize stages. Reports errors with file:line context.

Exit codes (sysexits.h):
  0   — success
  64  — config error
  65  — spec syntax / validation / resolution error
  69  — spec feature not supported
  70  — pipeline / internal error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from specmcp.cli.app import app
from specmcp.errors import SpecmcpError, exit_code_for


@app.command("validate")
def validate_cmd(
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
        help="Emit machine-readable JSON output.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show verbose output including debug details.",
    ),
) -> None:
    """Validate the spec and config. Exit 0 on success, non-zero on error."""
    from specmcp.config import Config
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.errors import ConfigError

    errors: list[dict] = []
    warnings: list[str] = []

    # 1. Load config
    cfg: Optional[Config] = None  # noqa: UP007
    if config and config.exists():
        try:
            cfg = Config.load(config)
        except SpecmcpError as exc:
            _emit_error(exc, json_output)
            raise typer.Exit(exit_code_for(exc))
    elif config and not config.exists() and spec is None:
        err = ConfigError(f"Config file not found: {config}. Use --spec to specify a spec directly.")
        _emit_error(err, json_output)
        raise typer.Exit(exit_code_for(err))

    # Resolve spec source
    spec_source: str
    if spec:
        spec_source = spec
    elif cfg:
        spec_source = cfg.spec.source
    else:
        typer.echo("Error: provide --spec <path-or-url> or a config file.", err=True)
        raise typer.Exit(64)

    # 2. Load + parse the spec
    try:
        raw, resolved = load_spec(spec_source)
    except SpecmcpError as exc:
        _emit_error(exc, json_output)
        raise typer.Exit(exit_code_for(exc))

    # 3. Normalize
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

    # 4. Check auth env vars (warn if missing, don't exit — validate is informational)
    if cfg and cfg.auth:
        try:
            cfg.resolve_auth_values()
        except SpecmcpError as exc:
            warnings.append(f"Auth warning: {exc.message}")

    # 5. Report
    if json_output:
        typer.echo(json.dumps({
            "status": "ok",
            "spec": spec_source,
            "openapi_version": resolved.openapi_version,
            "operation_count": len(ops),
            "warnings": warnings,
        }, indent=2))
    else:
        typer.echo(f"✓ Spec valid: {spec_source}")
        typer.echo(f"  OpenAPI version : {resolved.openapi_version}")
        typer.echo(f"  Operations found: {len(ops)}")
        for w in warnings:
            typer.echo(f"  ⚠  {w}", err=True)


def _emit_error(exc: SpecmcpError, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"status": "error", **exc.to_dict()}, indent=2), err=True)
    else:
        loc = f"[{exc.location}] " if exc.location else ""
        typer.echo(f"Error: {loc}{exc.message}", err=True)
        if exc.detail:
            typer.echo(f"  {exc.detail}", err=True)
