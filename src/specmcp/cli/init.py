"""
specmcp init — scaffold mcp.config.yaml from a spec.

Algorithm (from design doc §4.12):
  1. Load and validate the spec.
  2. Detect declared securitySchemes.
  3. Generate mcp.config.yaml scaffold and .env.example.
  4. Print summary (N operations, M auth schemes, next steps).

Does NOT enumerate operations in the config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from specmcp.cli.app import app
from specmcp.errors import SpecmcpError, exit_code_for


@app.command("init")
def init_cmd(
    spec_source: str = typer.Argument(
        ...,
        metavar="SPEC",
        help="Path or URL to the OpenAPI spec.",
    ),
    config: Path = typer.Option(
        Path("mcp.config.yaml"),
        "--config",
        "-c",
        help="Output path for the generated config file.",
        show_default=True,
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite the config file if it already exists.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
    ),
) -> None:
    """Scaffold mcp.config.yaml from a spec. Writes config + .env.example."""
    from specmcp.config import Config
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize

    # Guard against accidental overwrite
    if config.exists() and not force:
        typer.echo(
            f"Error: {config} already exists. Use --force to overwrite.", err=True
        )
        raise typer.Exit(64)

    # Load + parse
    try:
        raw, resolved = load_spec(spec_source)
    except SpecmcpError as exc:
        typer.echo(f"Error: {exc.message}", err=True)
        if exc.detail and verbose:
            typer.echo(f"  {exc.detail}", err=True)
        raise typer.Exit(exit_code_for(exc))

    # Normalize (to count operations)
    try:
        ops = normalize(resolved)
    except SpecmcpError as exc:
        typer.echo(f"Error: {exc.message}", err=True)
        raise typer.Exit(exit_code_for(exc))

    # Detect security schemes
    security_schemes = resolved.data.get("components", {}).get("securitySchemes", {})
    auth_scheme_list: list[dict] = []
    env_vars: list[str] = []

    for scheme_name, scheme_def in security_schemes.items():
        scheme_type = scheme_def.get("type", "")
        from specmcp.config import scheme_name_to_env_var
        env_var = scheme_name_to_env_var(scheme_name)
        env_vars.append(env_var)

        if scheme_type == "apiKey":
            auth_scheme_list.append({
                "name": scheme_name,
                "type": "apiKey",
                "in": scheme_def.get("in", "header"),
                "header_name": scheme_def.get("name", "X-Api-Key"),
            })
        elif scheme_type == "http" and scheme_def.get("scheme", "").lower() == "bearer":
            auth_scheme_list.append({
                "name": scheme_name,
                "type": "http",
                "scheme": "bearer",
            })
        else:
            # Unsupported — will be commented out in scaffold
            auth_scheme_list.append({
                "name": scheme_name,
                "type": scheme_type,
            })

    # Generate config scaffold
    config_yaml = Config.scaffold(spec_source, auth_scheme_list)
    config.write_text(config_yaml, encoding="utf-8")

    # Generate .env.example
    env_example_path = config.parent / ".env.example"
    env_lines = [
        "# specmcp — required environment variables",
        "# Copy to .env and fill in real values.",
        "",
    ]
    for var in env_vars:
        env_lines.append(f"{var}=your_{var.lower()}_here")
    env_example_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # Print summary
    n_supported = sum(
        1 for s in auth_scheme_list
        if s.get("type") in ("apiKey",) or (s.get("type") == "http" and s.get("scheme") == "bearer")
    )
    n_unsupported = len(auth_scheme_list) - n_supported

    typer.echo(f"✓ Generated {config}")
    typer.echo(f"  OpenAPI version  : {resolved.openapi_version}")
    typer.echo(f"  Operations found : {len(ops)}")
    typer.echo(f"  Auth schemes     : {n_supported} supported, {n_unsupported} unsupported (commented out)")
    if env_vars:
        typer.echo(f"  .env.example     : {env_example_path}")
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo(f"  1. Copy .env.example → .env and fill in your API keys.")
        typer.echo(f"  2. Run: specmcp validate --config {config}")
        typer.echo(f"  3. Run: specmcp serve --config {config}")
    else:
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo(f"  1. Run: specmcp validate --config {config}")
        typer.echo(f"  2. Run: specmcp serve --config {config}")
