"""
specmcp serve — run the MCP proxy server.

Runs the full pipeline once at startup:
  Load → Normalize → Simplify → Expose → ToolRegistry

Then starts an MCP server (stdio by default, HTTP optional) that answers:
  tools/list  → ToolRegistry.list_tools()
  tools/call  → Dispatcher.dispatch() → upstream API → MCP content blocks

HttpClient and AuthInjector are initialised once and shared for the server's
lifetime, giving connection pooling across tool calls.

--watch mode:
  When --watch is passed, a sibling anyio task monitors the spec and config
  files for changes and atomically reloads the ToolRegistry without dropping
  the stdio connection. AuthInjector is NOT rebuilt on reload — changes to
  the auth section of mcp.config.yaml require a server restart.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

import anyio
import typer

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from specmcp.cli.app import app
from specmcp.errors import (
    SpecmcpError,
    exit_code_for,
    mcp_error_content,
    is_transient,
)


# ---------------------------------------------------------------------------
# Pipeline helper (shared by startup and --watch reloader)
# ---------------------------------------------------------------------------


def _run_pipeline(
    spec_source: str,
    cfg: Any,  # Config | None
) -> Any:  # ToolRegistry
    """Load → Normalize → Simplify → Expose. Returns a fresh ToolRegistry.

    Raises SpecmcpError on any pipeline failure. The --watch reloader catches
    this and keeps the previous registry live rather than crashing the server.
    """
    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    _, resolved = load_spec(spec_source)
    ops = normalize(
        resolved,
        base_url_override=cfg.server.base_url_override if cfg else None,
        include_deprecated=cfg.server.include_deprecated if cfg else True,
        include_tags=cfg.server.include_tags if cfg else None,
        exclude_tags=cfg.server.exclude_tags if cfg else None,
        include_operations=cfg.server.include_operations if cfg else None,
        exclude_operations=cfg.server.exclude_operations if cfg else None,
    )
    simplify_cfg = cfg.simplify if cfg else SimplifyConfig()
    simplified = simplify(ops, config=simplify_cfg)
    return ToolRegistry.build(simplified, config=cfg)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@app.command("serve")
def serve_cmd(
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
    transport: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--transport",
        "-t",
        help="Transport protocol. Currently only 'stdio' is supported. ('http' is planned for a future release.)",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help=(
            "Reload the ToolRegistry when the spec or config file changes. "
            "Only works for local file specs; a warning is emitted to stderr for URL specs. "
            "Note: changes to the 'auth:' section require a server restart."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose (DEBUG-level) logging. Logs each tool call, auth injection, and HTTP request.",
    ),
) -> None:
    """Start the MCP proxy server."""
    import logging

    from specmcp.config import Config, DispatchConfig
    from specmcp.errors import SpecmcpError

    # --- Configure logging ---
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(level=logging.WARNING)

    # --- Load config ---
    cfg: Optional[Config] = None  # noqa: UP007
    if config and config.exists():
        try:
            cfg = Config.load(config)
        except SpecmcpError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(exit_code_for(exc))

    spec_source: str
    if spec:
        spec_source = spec
    elif cfg:
        spec_source = cfg.spec.source
    else:
        typer.echo("Error: provide --spec <path-or-url> or a config file.", err=True)
        raise typer.Exit(64)

    # --- Full pipeline (initial load) ---
    try:
        registry = _run_pipeline(spec_source, cfg)
    except SpecmcpError as exc:
        typer.echo(f"Error loading spec: {exc}", err=True)
        raise typer.Exit(exit_code_for(exc))

    if not registry.tools:
        typer.echo("Warning: no tools exposed. Check your spec and config filters.", err=True)

    # --- Resolve auth at startup (fails fast on missing env vars) ---
    from specmcp.auth.injector import AuthInjector
    try:
        auth_injector = AuthInjector.build(cfg)
    except SpecmcpError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(exit_code_for(exc))

    # --- Determine transport ---
    use_transport = transport
    if use_transport is None:
        use_transport = cfg.transport.default if cfg else "stdio"

    dispatch_cfg = cfg.dispatch if cfg else DispatchConfig()

    # --- Run server ---
    typer.echo(
        f"specmcp serving {len(registry.tools)} tools from {spec_source} "
        f"[transport={use_transport}]"
        + (" [watch]" if watch else ""),
        err=True,
    )

    anyio.run(
        _run_server,
        registry,
        auth_injector,
        dispatch_cfg,
        cfg,
        use_transport,
        watch,
        config,
        spec_source,
    )


# ---------------------------------------------------------------------------
# Async server loop
# ---------------------------------------------------------------------------


async def _run_server(
    registry: Any,
    auth_injector: Any,
    dispatch_cfg: Any,
    cfg: Any,
    transport: str,
    watch: bool,
    config_path: Optional[Path],
    spec_source: str,
) -> None:
    from specmcp.runtime.http_client import HttpClient
    from specmcp.runtime.dispatcher import dispatch as dispatch_tool
    from specmcp.runtime.registry_ref import RegistryRef

    registry_ref = RegistryRef(registry)
    server = Server("specmcp")

    # HttpClient is opened once here and shared across all tool calls so that
    # connection pooling works correctly. Opening a new client per call would
    # pay a full TCP + TLS handshake on every tools/call request.
    async with HttpClient(dispatch_cfg) as http_client:

        # --- tools/list handler ---
        @server.list_tools()
        async def handle_list_tools() -> list[mcp_types.Tool]:
            reg = registry_ref.get()
            return [
                mcp_types.Tool(
                    name=tool.name,
                    description=tool.description,
                    inputSchema=tool.input_schema,
                )
                for tool in reg.tools
            ]

        # --- tools/call handler ---
        @server.call_tool()
        async def handle_call_tool(
            name: str,
            arguments: dict[str, Any],
        ) -> list[mcp_types.TextContent]:
            request_id = str(uuid.uuid4())[:8]
            reg = registry_ref.get()

            tool = reg.lookup(name)
            if tool is None:
                return [mcp_types.TextContent(
                    type="text",
                    text=f"Unknown tool: {name!r}",
                )]

            op_override = None
            if cfg and name in cfg.operations:
                op_override = cfg.operations[name]
            elif cfg and tool.simplified_operation.operation.id in cfg.operations:
                op_override = cfg.operations[tool.simplified_operation.operation.id]

            try:
                content_blocks = await dispatch_tool(
                    tool=tool,
                    llm_args=arguments or {},
                    http_client=http_client,
                    auth_injector=auth_injector,
                    dispatch_config=dispatch_cfg,
                    operation_override=op_override,
                    request_id=request_id,
                )
                return [
                    mcp_types.TextContent(type="text", text=block["text"])
                    for block in content_blocks
                    if block.get("type") == "text"
                ]
            except SpecmcpError as exc:
                exc.request_id = exc.request_id or request_id
                error_text = mcp_error_content(exc)
                if is_transient(exc):
                    pass  # meta["transient"] surfaced via error_text
                return [mcp_types.TextContent(type="text", text=error_text)]

        # --- Start transport + optional watcher as sibling tasks ---
        async with anyio.create_task_group() as tg:
            if transport == "stdio":
                tg.start_soon(_run_stdio, server)
            elif transport == "http":
                typer.echo(
                    "HTTP transport is not yet implemented in v1. Use --transport stdio.",
                    err=True,
                )
                raise SystemExit(1)
            else:
                typer.echo(f"Unknown transport: {transport!r}. Use 'stdio' (only supported transport in v1).", err=True)
                raise SystemExit(1)

            if watch:
                tg.start_soon(
                    _watch_and_reload,
                    spec_source,
                    config_path,
                    registry_ref,
                    cfg,
                )


async def _run_stdio(server: Server) -> None:
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


# ---------------------------------------------------------------------------
# File watcher (--watch mode)
# ---------------------------------------------------------------------------


async def _watch_and_reload(
    spec_source: str,
    config_path: Optional[Path],
    registry_ref: Any,  # RegistryRef
    cfg: Any,  # Config | None
) -> None:
    """Monitor spec and config files and atomically reload the ToolRegistry on changes.

    Silently no-ops if spec_source is a URL (cannot watch remote files).
    If a reload fails (e.g. the spec is temporarily invalid mid-save), the
    previous registry is kept live and a warning is emitted to stderr.

    Note: AuthInjector is NOT rebuilt on reload. Changes to the 'auth:' section
    of mcp.config.yaml require a full server restart.
    """
    try:
        from watchfiles import awatch
    except ImportError:
        typer.echo(
            "Warning: --watch requires 'watchfiles'. Install it with: pip install watchfiles",
            err=True,
        )
        return

    paths_to_watch: set[str] = set()

    spec_path = Path(spec_source)
    if spec_path.exists():
        paths_to_watch.add(str(spec_path.resolve()))
    else:
        typer.echo(
            f"[watch] spec source {spec_source!r} is a URL or does not exist locally — "
            "file watching disabled. Restart the server to pick up spec changes.",
            err=True,
        )

    if config_path and config_path.exists():
        paths_to_watch.add(str(config_path.resolve()))

    if not paths_to_watch:
        return

    typer.echo(
        f"[watch] watching {', '.join(sorted(paths_to_watch))}",
        err=True,
    )

    # Resolve config path to a string for change detection comparisons.
    resolved_config_path = str(config_path.resolve()) if config_path and config_path.exists() else None

    async for changes in awatch(*paths_to_watch):
        changed_files = ", ".join(str(c[1]) for c in changes)
        typer.echo(f"[watch] change detected in {changed_files} — reloading...", err=True)

        # Warn if the config file itself changed — auth section changes are not
        # picked up by the watcher (AuthInjector is not rebuilt on reload).
        config_changed = resolved_config_path and any(
            str(c[1]) == resolved_config_path for c in changes
        )
        if config_changed:
            typer.echo(
                "[watch] Note: changes to the 'auth:' section of the config file are NOT "
                "applied on hot-reload. Restart the server to pick up auth changes.",
                err=True,
            )

        try:
            new_registry = _run_pipeline(spec_source, cfg)
            await registry_ref.swap(new_registry)
            typer.echo(
                f"[watch] reloaded {len(new_registry.tools)} tools successfully.",
                err=True,
            )
        except SpecmcpError as exc:
            typer.echo(
                f"[watch] reload failed: {exc} — keeping previous registry.",
                err=True,
            )
