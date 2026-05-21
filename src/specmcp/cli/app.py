"""
specmcp CLI — top-level Typer application.

Commands:
  init      Scaffold mcp.config.yaml from a spec.
  validate  Validate the spec and config without serving.
  inspect   List the tools that would be exposed.
  serve     Run the MCP proxy server.
  report-issue  Bundle a sanitized debug report.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from specmcp import __version__

app = typer.Typer(
    name="specmcp",
    help="Convert any OpenAPI spec into a working MCP server.",
    add_completion=False,
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"specmcp {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(  # noqa: UP007
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """specmcp — Convert any OpenAPI spec into a working MCP server."""


# Import sub-commands (registers them on ``app``)
from specmcp.cli.validate import validate_cmd  # noqa: E402, F401
from specmcp.cli.init import init_cmd          # noqa: E402, F401
from specmcp.cli.inspect import inspect_cmd   # noqa: E402, F401
from specmcp.cli.serve import serve_cmd        # noqa: E402, F401
from specmcp.cli.report_issue import report_issue_cmd  # noqa: E402, F401
