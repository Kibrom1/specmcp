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

HTTP transport:
  When --transport http is passed, a Starlette ASGI app is started with uvicorn.
  MCP is served over SSE at GET /sse and POST /messages.
  Each HTTP connection gets its own SessionContext (supporting per-session auth).
  OAuth callback routes (/auth/*) are mounted on the same app in Phase 4.
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
# Module-level session map
# ---------------------------------------------------------------------------
# Maps session_id -> SessionContext for all active sessions.
# For stdio transport this holds exactly one entry (single session per process).
# For HTTP transport (Phase 4), one entry per connected client.
#
# Import is deferred to avoid circular imports during module load.
_sessions: dict[str, "Any"] = {}  # dict[str, SessionContext]


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
        help="Transport protocol: 'stdio' (default) or 'http' (SSE on /sse and /messages).",
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
    management_port: Optional[int] = typer.Option(  # noqa: UP007
        None,
        "--management-port",
        help=(
            "Override management.port in the config (default: 8766). "
            "NOTE: this field is currently reserved — management routes run on the same "
            "port as the HTTP transport and this value has no routing effect yet. "
            "It will take effect in a future release when management gets a dedicated listener."
        ),
    ),
    management_bind: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--management-bind",
        help=(
            "Override management endpoint bind: 'loopback' (default, safe for single-host) "
            "or 'all' (reachable from any interface — requires management_token_from in config). "
            "Only relevant with HTTP transport and OAuth2 authorization_code schemes."
        ),
    ),
    token_store: str = typer.Option(
        "memory",
        "--token-store",
        help=(
            "Token persistence backend: 'memory' (default, lost on restart) or 'sqlite' "
            "(encrypted at rest, survives restart). Only relevant with HTTP transport and "
            "OAuth2 authorization_code schemes."
        ),
    ),
    token_store_path: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--token-store-path",
        help=(
            "Base path for the SQLite token database (default: ~/.specmcp/tokens.db). "
            "For multi-scheme configs each scheme gets its own file derived from this "
            "path (e.g. tokens_myScheme.db beside tokens.db). "
            "Only used when --token-store sqlite is set."
        ),
    ),
    token_store_key_env: str = typer.Option(
        "SPECMCP_TOKEN_KEY",
        "--token-store-key-env",
        help=(
            "Name of the environment variable that holds the encryption key for the SQLite "
            "token store (default: SPECMCP_TOKEN_KEY). The variable's value is used as key "
            "material (any non-empty string). Only used when --token-store sqlite is set."
        ),
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

    # --- Apply management config overrides (--management-port / --management-bind) ---
    if management_bind is not None and management_bind not in ("loopback", "all"):
        typer.echo(
            f"Error: --management-bind must be 'loopback' or 'all', got {management_bind!r}.",
            err=True,
        )
        raise typer.Exit(64)

    if management_port is not None:
        typer.echo(
            "Warning: --management-port sets management.port in the config but currently has "
            "no routing effect — management routes run on the HTTP transport port. "
            "This will take effect in a future release.",
            err=True,
        )

    if cfg is not None and (management_port is not None or management_bind is not None):
        from specmcp.config import ManagementConfig
        try:
            cfg.management = ManagementConfig(
                bind=management_bind if management_bind is not None else cfg.management.bind,
                port=management_port if management_port is not None else cfg.management.port,
                management_token_from=cfg.management.management_token_from,
            )
        except Exception as exc:  # pydantic ValidationError
            typer.echo(f"Error: invalid management config: {exc}", err=True)
            raise typer.Exit(64)
    elif cfg is None and (management_port is not None or management_bind is not None):
        typer.echo(
            "Warning: --management-port / --management-bind have no effect without a config file.",
            err=True,
        )

    # --- Validate and resolve token store config ---
    import os as _os

    if token_store not in ("memory", "sqlite"):
        typer.echo(
            f"Error: --token-store must be 'memory' or 'sqlite', got {token_store!r}.",
            err=True,
        )
        raise typer.Exit(64)

    sqlite_db_path: Optional[Path] = None  # noqa: UP007
    sqlite_key_bytes: Optional[bytes] = None  # noqa: UP007

    if token_store == "sqlite":
        # Resolve db path
        sqlite_db_path = token_store_path or Path.home() / ".specmcp" / "tokens.db"

        # Resolve encryption key from env var
        key_raw = _os.environ.get(token_store_key_env)
        if not key_raw:
            typer.echo(
                f"Error: --token-store sqlite requires the {token_store_key_env!r} environment "
                "variable to be set with the encryption key material.",
                err=True,
            )
            raise typer.Exit(64)
        sqlite_key_bytes = key_raw.encode()

        # Warn if the key material is suspiciously short.
        # HKDF will still work, but short passphrases have low entropy.
        # 16 bytes ≈ 128-bit minimum; 32+ bytes is recommended.
        if len(sqlite_key_bytes) < 16:
            typer.echo(
                f"Warning: {token_store_key_env!r} is only {len(sqlite_key_bytes)} bytes — "
                "consider using at least 16 characters for adequate key strength. "
                "The store will still be created, but encryption strength is limited.",
                err=True,
            )

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
    http_addr = ""
    if use_transport == "http":
        http_cfg = cfg.transport.http if cfg else None
        _host = http_cfg.host if http_cfg else "127.0.0.1"
        _port = http_cfg.port if http_cfg else 8765
        http_addr = f" http://{_host}:{_port}"

    typer.echo(
        f"specmcp serving {len(registry.tools)} tools from {spec_source} "
        f"[transport={use_transport}{http_addr}]"
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
        token_store,
        sqlite_db_path,
        sqlite_key_bytes,
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
    token_store_type: str = "memory",
    sqlite_db_path: Optional[Path] = None,
    sqlite_key_bytes: Optional[bytes] = None,
) -> None:
    from specmcp.config import SensitiveStr
    from specmcp.runtime.http_client import HttpClient
    from specmcp.runtime.dispatcher import dispatch as dispatch_tool
    from specmcp.runtime.registry_ref import RegistryRef
    from specmcp.runtime.session import SessionContext

    registry_ref = RegistryRef(registry)

    # HttpClient is opened once here and shared across all tool calls so that
    # connection pooling works correctly. Opening a new client per call would
    # pay a full TCP + TLS handshake on every tools/call request.
    async with HttpClient(dispatch_cfg) as http_client:

        if transport == "stdio":
            server = Server("specmcp")

            # One SessionContext per stdio connection — holds this session's identity
            # and optional client-supplied bearer token.
            stdio_session = SessionContext(session_id=str(uuid.uuid4()))
            _sessions[stdio_session.session_id] = stdio_session

            # Lazy, once-per-connection flag: the MCP initialize request arrives
            # before the first tool call, but the SDK only exposes it via
            # request_ctx. We read the client token on the first handler
            # invocation and never again.
            _client_token_read = False

            def _maybe_read_client_token() -> None:
                nonlocal _client_token_read
                if _client_token_read:
                    return
                _client_token_read = True
                try:
                    from mcp.server.lowlevel.server import request_ctx  # type: ignore[import]
                    ctx = request_ctx.get()
                    params = ctx.session.client_params  # type: ignore[union-attr]
                    if params and params.meta:
                        raw_token = params.meta.model_extra.get("bearer_token")  # type: ignore[union-attr]
                        if raw_token and isinstance(raw_token, str):
                            stdio_session.client_token = SensitiveStr(raw_token)
                except (LookupError, AttributeError):
                    # request_ctx not set, or the MCP SDK version differs — safe to ignore.
                    pass

            # --- tools/list handler ---
            @server.list_tools()
            async def handle_list_tools() -> list[mcp_types.Tool]:
                _maybe_read_client_token()
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
                _maybe_read_client_token()
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
                        session=stdio_session,
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

            async with anyio.create_task_group() as tg:
                tg.start_soon(_run_stdio, server)
                if watch:
                    tg.start_soon(
                        _watch_and_reload,
                        spec_source,
                        config_path,
                        registry_ref,
                        cfg,
                    )

        elif transport == "http":
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    _run_http,
                    registry_ref,
                    http_client,
                    auth_injector,
                    dispatch_cfg,
                    cfg,
                    token_store_type,
                    sqlite_db_path,
                    sqlite_key_bytes,
                )
                if watch:
                    tg.start_soon(
                        _watch_and_reload,
                        spec_source,
                        config_path,
                        registry_ref,
                        cfg,
                    )

        else:
            typer.echo(
                f"Unknown transport: {transport!r}. Supported transports: 'stdio', 'http'.",
                err=True,
            )
            raise SystemExit(1)


async def _run_stdio(server: Server) -> None:
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def _build_oauth_state(
    cfg: Any,
    auth_injector: Any,
    login_base_url: str,
    *,
    token_store_type: str = "memory",
    sqlite_db_path: "Optional[Path]" = None,
    sqlite_key_bytes: "Optional[bytes]" = None,
) -> "Any | None":
    """Create OAuthHandlerState and register AuthCodeHandlers with the injector.

    Called at HTTP server startup when at least one oauth2_authorization_code
    scheme is configured. Returns the OAuthHandlerState (for mounting routes)
    or None if no auth code schemes are present.

    Steps:
      1. Resolve credentials for each scheme from env vars.
      2. Create a token store per scheme (InMemoryTokenStore or SqliteTokenStore).
      3. Build OAuthHandlerState (holds nonce store + PKCE store).
      4. Build an AuthCodeHandler per scheme (uses oauth_state.issue_nonce).
      5. Register each AuthCodeHandler with the injector.

    Token stores created here are NOT yet opened (SqliteTokenStore requires
    async open()); the caller (_run_http) is responsible for calling
    ``await store.open()`` on each ``resolved_scheme.token_store`` before
    serving requests and ``await store.close()`` on shutdown.
    """
    import secrets

    from specmcp.auth.oauth2_authcode import AuthCodeHandler
    from specmcp.auth.token_store import InMemoryTokenStore, SqliteTokenStore
    from specmcp.config import OAuth2AuthorizationCodeConfig, _resolve_value_from
    from specmcp.runtime.oauth_handler import OAuthHandlerState, ResolvedAuthCodeScheme

    if cfg is None:
        return None

    auth_code_schemes = {
        name: scheme_cfg
        for name, scheme_cfg in cfg._auth_schemes.items()  # noqa: SLF001
        if isinstance(scheme_cfg, OAuth2AuthorizationCodeConfig)
    }
    if not auth_code_schemes:
        return None

    # Step 1+2: Resolve credentials and create token stores
    resolved_schemes: dict[str, ResolvedAuthCodeScheme] = {}
    for scheme_name, scheme_cfg in auth_code_schemes.items():
        client_id = _resolve_value_from(scheme_cfg.client_id_from, scheme_name)
        client_secret = _resolve_value_from(scheme_cfg.client_secret_from, scheme_name)

        if token_store_type == "sqlite":
            assert sqlite_db_path is not None, "sqlite_db_path required for sqlite token store"
            assert sqlite_key_bytes is not None, "sqlite_key_bytes required for sqlite token store"
            # Use a per-scheme sub-path so different schemes don't share rows
            scheme_db_path = sqlite_db_path.parent / f"{sqlite_db_path.stem}_{scheme_name}{sqlite_db_path.suffix}"
            token_store: Any = SqliteTokenStore(scheme_db_path, sqlite_key_bytes)
        else:
            token_store = InMemoryTokenStore()

        resolved_schemes[scheme_name] = ResolvedAuthCodeScheme(
            scheme_name=scheme_name,
            config=scheme_cfg,
            client_id=client_id,
            client_secret=client_secret,
            token_store=token_store,
        )

    # Step 3: Management access config
    mgmt = cfg.management
    management_bind_all = mgmt.bind == "all"
    management_token = None
    if mgmt.management_token_from:
        from specmcp.config import _resolve_value_from as _rv
        management_token = _rv(mgmt.management_token_from, "management")

    oauth_state = OAuthHandlerState(
        schemes=resolved_schemes,
        server_secret=secrets.token_hex(32),
        management_bind_all=management_bind_all,
        management_token=management_token,
    )

    # Step 4+5: Build AuthCodeHandler per scheme and register with the injector
    for scheme_name, resolved in resolved_schemes.items():
        handler = AuthCodeHandler(
            scheme_name=scheme_name,
            config=resolved.config,
            client_id=resolved.client_id,
            client_secret=resolved.client_secret,
            token_store=resolved.token_store,
            issue_nonce=oauth_state.issue_nonce,
            login_base_url=login_base_url,
        )
        auth_injector.register_auth_code_handler(scheme_name, handler)

    return oauth_state


async def _run_http(
    registry_ref: Any,
    http_client: Any,
    auth_injector: Any,
    dispatch_cfg: Any,
    cfg: Any,
    token_store_type: str = "memory",
    sqlite_db_path: Optional[Path] = None,
    sqlite_key_bytes: Optional[bytes] = None,
) -> None:
    """Run the MCP server over HTTP/SSE via Starlette + uvicorn.

    Each SSE connection spawns its own MCP Server instance and SessionContext
    so that per-session state (auth tokens, etc.) is isolated.

    Routes (always present):
      GET  /sse      — SSE stream; client subscribes here to receive MCP messages
      POST /messages — client POSTs MCP requests here (session_id in query string)

    Routes (added when oauth2_authorization_code schemes are configured):
      GET    /auth/login?nonce=<token>    — consume nonce, redirect to upstream IdP
      GET    /auth/callback?code=&state=  — exchange code for tokens, show success page
      GET    /auth/status?session=<id>    — poll: {"authenticated": true|false}
      DELETE /auth/session/<id>           — revoke tokens (management endpoint)
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Route

    from specmcp.config import SensitiveStr
    from specmcp.runtime.dispatcher import dispatch as dispatch_tool
    from specmcp.runtime.session import SessionContext

    host = cfg.transport.http.host if cfg else "127.0.0.1"
    port = cfg.transport.http.port if cfg else 8765

    # Wire OAuth state + inject AuthCodeHandlers into the injector (if any auth code
    # schemes are configured in the config).
    login_base_url = f"http://{host}:{port}"
    oauth_state = _build_oauth_state(
        cfg,
        auth_injector,
        login_base_url,
        token_store_type=token_store_type,
        sqlite_db_path=sqlite_db_path,
        sqlite_key_bytes=sqlite_key_bytes,
    )

    # Open token stores (no-op for InMemoryTokenStore; connects DB for SqliteTokenStore).
    token_stores = (
        [resolved.token_store for resolved in oauth_state.schemes.values()]
        if oauth_state
        else []
    )
    for _ts in token_stores:
        await _ts.open()

    sse_transport = SseServerTransport("/messages")

    async def _handle_sse(request: Request) -> None:
        """Handle one SSE connection — creates a dedicated session + MCP server."""
        session = SessionContext(session_id=str(uuid.uuid4()))
        _sessions[session.session_id] = session

        _client_token_read = False

        def _maybe_read_client_token() -> None:
            nonlocal _client_token_read
            if _client_token_read:
                return
            _client_token_read = True
            try:
                from mcp.server.lowlevel.server import request_ctx  # type: ignore[import]
                ctx = request_ctx.get()
                params = ctx.session.client_params  # type: ignore[union-attr]
                if params and params.meta:
                    raw_token = params.meta.model_extra.get("bearer_token")  # type: ignore[union-attr]
                    if raw_token and isinstance(raw_token, str):
                        session.client_token = SensitiveStr(raw_token)
            except (LookupError, AttributeError):
                pass

        conn_server = Server("specmcp")

        @conn_server.list_tools()
        async def handle_list_tools() -> list[mcp_types.Tool]:
            _maybe_read_client_token()
            reg = registry_ref.get()
            return [
                mcp_types.Tool(
                    name=tool.name,
                    description=tool.description,
                    inputSchema=tool.input_schema,
                )
                for tool in reg.tools
            ]

        @conn_server.call_tool()
        async def handle_call_tool(
            name: str,
            arguments: dict[str, Any],
        ) -> list[mcp_types.TextContent]:
            _maybe_read_client_token()
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
                    session=session,
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
                    pass
                return [mcp_types.TextContent(type="text", text=error_text)]

        try:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                init_options = conn_server.create_initialization_options()
                await conn_server.run(read_stream, write_stream, init_options)
        finally:
            _sessions.pop(session.session_id, None)

    # Mount OAuth callback routes alongside the MCP SSE routes when an
    # oauth2_authorization_code scheme is configured.
    from specmcp.runtime.oauth_handler import build_oauth_routes
    oauth_routes = build_oauth_routes(oauth_state) if oauth_state else []

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=_handle_sse),
            Route(
                "/messages",
                endpoint=sse_transport.handle_post_message,
                methods=["POST"],
            ),
            *oauth_routes,
        ]
    )

    uvicorn_config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    try:
        await uvicorn_server.serve()
    finally:
        # Close token stores on shutdown (no-op for InMemoryTokenStore).
        for _ts in token_stores:
            await _ts.close()


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
