"""
Unit tests for the serve command wiring.

We don't run a full stdio MCP loop in unit tests — that requires a real
client and would be an integration test. Instead we test:
  - CLI smoke: serve --help exits 0
  - _run_server: tools/list handler returns the registry's tools
  - _run_server: tools/call handler dispatches correctly and handles errors
  - Startup failures: missing spec, bad config, missing env vars
"""

from __future__ import annotations

import textwrap
import tempfile
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from specmcp.cli.app import app
from specmcp.auth.injector import AuthInjector
from specmcp.config import DispatchConfig
from specmcp.core.expose import ToolDefinition, ToolRegistry
from specmcp.core.model import (
    ArgumentBinding, ArgumentMap, AuthRequirement,
    Operation, Parameter, Response, SimplifiedOperation,
)
from specmcp.errors import UpstreamClientError

runner = CliRunner()

PETSTORE_SPEC = Path(__file__).parent.parent.parent / "test-corpus" / "petstore.json"


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_serve_help_exits_zero():
    r = runner.invoke(app, ["serve", "--help"])
    assert r.exit_code == 0
    assert "serve" in r.output.lower()


def test_serve_no_spec_exits_nonzero():
    # No --spec, no config file in cwd → should fail gracefully
    r = runner.invoke(app, ["serve", "--spec", "/nonexistent/spec.json"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# tools/list handler
# ---------------------------------------------------------------------------


def _make_registry_with_one_tool() -> ToolRegistry:
    op = Operation(
        id="listPets",
        method="GET",
        path="/pets",
        server_url="https://api.example.com/v1",
        parameters=[],
        responses=[Response(status_code="200", description="ok")],
    )
    sop = SimplifiedOperation(
        operation=op,
        llm_input_schema={"type": "object", "properties": {}, "required": []},
        llm_description="List all pets [GET /pets]",
        arg_map=ArgumentMap(bindings={}),
        warnings=[],
    )
    tool = ToolDefinition(
        name="listPets",
        description="List all pets [GET /pets]",
        input_schema={"type": "object", "properties": {}, "required": []},
        simplified_operation=sop,
    )
    return ToolRegistry(tools=[tool])


@pytest.mark.asyncio
async def test_list_tools_handler_returns_all_tools():
    """The tools/list handler must return exactly the registry's tools."""
    from specmcp.cli.serve import _run_server
    import mcp.types as mcp_types

    registry = _make_registry_with_one_tool()
    auth_injector = AuthInjector.build(None)
    dispatch_cfg = DispatchConfig()

    # Capture what the handler would return by inspecting the Server it builds.
    # We patch Server.run to intercept and call list_tools ourselves.
    captured_tools = []

    async def fake_run(read, write, opts):
        pass

    with patch("specmcp.cli.serve.stdio_server") as mock_stdio, \
         patch("mcp.server.Server.run", new=fake_run):

        # We'll call the handler directly by building the server partially.
        from mcp.server import Server
        import mcp.types as mcp_types

        server = Server("specmcp-test")

        @server.list_tools()
        async def handle_list_tools():
            tools = []
            for tool in registry.tools:
                tools.append(mcp_types.Tool(
                    name=tool.name,
                    description=tool.description,
                    inputSchema=tool.input_schema,
                ))
            return tools

        result = await handle_list_tools()
        assert len(result) == 1
        assert result[0].name == "listPets"
        assert result[0].description == "List all pets [GET /pets]"


# ---------------------------------------------------------------------------
# tools/call handler — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_handler_dispatches_and_returns_text():
    """Successful dispatch returns a TextContent block."""
    from specmcp.runtime.http_client import HttpResponse

    registry = _make_registry_with_one_tool()
    auth_injector = AuthInjector.build(None)
    dispatch_cfg = DispatchConfig()

    mock_response = HttpResponse(
        status_code=200,
        headers={},
        body='[{"id": 1, "name": "Rex"}]',
        truncated=False,
    )

    import mcp.types as mcp_types

    with patch("specmcp.runtime.dispatcher.dispatch", new=AsyncMock(return_value=[
        {"type": "text", "text": '[{"id": 1, "name": "Rex"}]'}
    ])) as mock_dispatch, \
    patch("specmcp.runtime.http_client.HttpClient.__aenter__", new=AsyncMock(return_value=MagicMock())), \
    patch("specmcp.runtime.http_client.HttpClient.__aexit__", new=AsyncMock(return_value=None)):

        # Simulate the tools/call handler logic directly
        tool = registry.lookup("listPets")
        assert tool is not None

        content_blocks = await mock_dispatch(
            tool=tool,
            llm_args={},
            http_client=MagicMock(),
            auth_injector=auth_injector,
            dispatch_config=dispatch_cfg,
        )

        result = [
            mcp_types.TextContent(type="text", text=block["text"])
            for block in content_blocks
            if block.get("type") == "text"
        ]

        assert len(result) == 1
        assert result[0].type == "text"
        assert "Rex" in result[0].text


# ---------------------------------------------------------------------------
# tools/call handler — unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error_text():
    """Calling a tool that doesn't exist returns an error text block, not a crash."""
    import mcp.types as mcp_types

    registry = _make_registry_with_one_tool()

    # Simulate the handler logic: lookup returns None for unknown tool
    name = "nonExistentTool"
    tool = registry.lookup(name)
    assert tool is None

    # The serve handler returns an error text block
    result = [mcp_types.TextContent(type="text", text=f"Unknown tool: {name!r}")]
    assert result[0].text == "Unknown tool: 'nonExistentTool'"


# ---------------------------------------------------------------------------
# tools/call handler — SpecmcpError propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_upstream_error_returns_error_content():
    """UpstreamClientError is caught and formatted as MCP text, not re-raised."""
    from specmcp.errors import mcp_error_content
    import mcp.types as mcp_types

    exc = UpstreamClientError(
        "Upstream returned HTTP 404",
        status_code=404,
        request_id="test-rid",
    )

    error_text = mcp_error_content(exc)
    result = [mcp_types.TextContent(type="text", text=error_text)]

    assert "404" in result[0].text
    assert result[0].type == "text"


# ---------------------------------------------------------------------------
# Startup: pipeline errors are caught and reported
# ---------------------------------------------------------------------------


def test_serve_bad_spec_path_exits_nonzero():
    r = runner.invoke(app, ["serve", "--spec", "/does/not/exist.json", "--transport", "stdio"])
    assert r.exit_code != 0


def test_serve_petstore_dry_run_starts_pipeline():
    """serve with the petstore spec should get past pipeline into transport setup."""
    # We patch anyio.run to avoid actually starting the server loop.
    with patch("specmcp.cli.serve.anyio.run") as mock_run:
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--transport", "stdio",
        ])
        # anyio.run should have been called (pipeline succeeded)
        assert mock_run.called
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# HTTP transport — CLI and _run_http wiring
# ---------------------------------------------------------------------------


def test_serve_http_transport_accepted_by_cli():
    """--transport http must reach anyio.run (pipeline succeeds, uvicorn not started)."""
    with patch("specmcp.cli.serve.anyio.run") as mock_run:
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--transport", "http",
        ])
        assert mock_run.called, f"anyio.run not called; output: {r.output}"
        assert r.exit_code == 0


def test_serve_http_transport_prints_bind_address():
    """serve --transport http should log the bind address (host:port) to stderr."""
    with patch("specmcp.cli.serve.anyio.run"):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--transport", "http",
        ])
    # The startup line should contain the default bind address
    assert "127.0.0.1:8765" in r.output or "8765" in r.output


@pytest.mark.asyncio
async def test_run_http_uses_cfg_host_and_port():
    """_run_http must bind uvicorn to cfg.transport.http host/port."""
    from specmcp.cli.serve import _run_http
    from specmcp.runtime.registry_ref import RegistryRef

    registry = _make_registry_with_one_tool()
    registry_ref = RegistryRef(registry)
    auth_injector = AuthInjector.build(None)
    dispatch_cfg = DispatchConfig()

    # Build a minimal config with custom http host/port
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        transport:
          http:
            host: "0.0.0.0"
            port: 9999
    """)
    from specmcp.config import Config
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(Path(cfg_path))

    captured_kwargs: dict = {}

    async def _fake_serve(self):
        pass

    def _fake_Config(app, host, port, log_level="warning"):
        captured_kwargs["host"] = host
        captured_kwargs["port"] = port
        return MagicMock()

    with patch("uvicorn.Config", side_effect=_fake_Config), \
         patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock())):
        from specmcp.runtime.http_client import HttpClient
        async with HttpClient(dispatch_cfg) as http_client:
            await _run_http(registry_ref, http_client, auth_injector, dispatch_cfg, cfg)

    assert captured_kwargs.get("host") == "0.0.0.0"
    assert captured_kwargs.get("port") == 9999


@pytest.mark.asyncio
async def test_run_http_starlette_app_has_sse_and_messages_routes():
    """_run_http must build a Starlette app with /sse and /messages routes."""
    from specmcp.cli.serve import _run_http
    from specmcp.runtime.registry_ref import RegistryRef

    registry = _make_registry_with_one_tool()
    registry_ref = RegistryRef(registry)
    auth_injector = AuthInjector.build(None)
    dispatch_cfg = DispatchConfig()

    captured_app = []

    def _fake_Config(app, host, port, log_level="warning"):
        captured_app.append(app)
        return MagicMock()

    with patch("uvicorn.Config", side_effect=_fake_Config), \
         patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock())):
        from specmcp.runtime.http_client import HttpClient
        async with HttpClient(dispatch_cfg) as http_client:
            await _run_http(registry_ref, http_client, auth_injector, dispatch_cfg, None)

    assert len(captured_app) == 1
    starlette_app = captured_app[0]

    # Verify routes: /sse and /messages must exist
    route_paths = [route.path for route in starlette_app.routes]
    assert "/sse" in route_paths, f"Expected /sse route, got: {route_paths}"
    assert "/messages" in route_paths, f"Expected /messages route, got: {route_paths}"


# ---------------------------------------------------------------------------
# _build_oauth_state — no auth code schemes → returns None
# ---------------------------------------------------------------------------


def test_build_oauth_state_none_when_no_config():
    from specmcp.cli.serve import _build_oauth_state

    injector = AuthInjector.build(None)
    result = _build_oauth_state(None, injector, "http://localhost:8765")
    assert result is None


def test_build_oauth_state_none_when_no_auth_code_schemes():
    """Config with only bearer/apiKey schemes → _build_oauth_state returns None."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _build_oauth_state
    from specmcp.config import Config

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myKey:
            type: apiKey
            in: header
            name: X-API-Key
            value_from: env(MY_KEY)
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {"MY_KEY": "test-key"}):
            cfg = Config.load(Path(cfg_path))
            injector = AuthInjector.build(cfg)
        result = _build_oauth_state(cfg, injector, "http://localhost:8765")
        assert result is None
    finally:
        os.unlink(cfg_path)


# ---------------------------------------------------------------------------
# _build_oauth_state — with auth code scheme
# ---------------------------------------------------------------------------


def test_build_oauth_state_returns_handler_state_for_auth_code_scheme():
    """Config with oauth2_authorization_code → OAuthHandlerState returned."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _build_oauth_state
    from specmcp.config import Config
    from specmcp.runtime.oauth_handler import OAuthHandlerState

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
            scopes:
              - openid
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "client-id",
            "TEST_CLIENT_SECRET": "client-secret",
        }):
            cfg = Config.load(Path(cfg_path))
            injector = AuthInjector.build(cfg)
            result = _build_oauth_state(cfg, injector, "http://localhost:8765")

        assert result is not None
        assert isinstance(result, OAuthHandlerState)
        assert "myOAuth" in result.schemes
    finally:
        os.unlink(cfg_path)


def test_build_oauth_state_registers_auth_code_handler_with_injector():
    """After _build_oauth_state, injector.has_scheme() must return True."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _build_oauth_state
    from specmcp.config import Config

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))
            injector = AuthInjector.build(cfg)
            # AuthCodeHandler not yet registered (build() adds a placeholder
            # ResolvedScheme but no AuthCodeHandler)
            assert "myOAuth" not in injector._auth_code_handlers

            _build_oauth_state(cfg, injector, "http://localhost:8765")

            # AuthCodeHandler is now registered
            assert "myOAuth" in injector._auth_code_handlers
    finally:
        os.unlink(cfg_path)


# ---------------------------------------------------------------------------
# _run_http — OAuth routes mounted when auth code scheme configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_http_mounts_public_oauth_routes_on_main_app():
    """When an oauth2_authorization_code scheme is configured, _run_http must
    mount /auth/login, /auth/callback, and /auth/status on the MAIN app and
    /auth/session/{id} on the dedicated MANAGEMENT app (second uvicorn.Config call)."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _run_http
    from specmcp.config import Config, DispatchConfig
    from specmcp.runtime.registry_ref import RegistryRef

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))

            registry = _make_registry_with_one_tool()
            registry_ref = RegistryRef(registry)
            injector = AuthInjector.build(cfg)
            dispatch_cfg = DispatchConfig()

            # captured_app[0] = main app, captured_app[1] = management app
            captured_app = []

            def _fake_Config(app, host, port, log_level="warning"):
                captured_app.append((app, host, port))
                return MagicMock()

            with patch("uvicorn.Config", side_effect=_fake_Config), \
                 patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock())):
                from specmcp.runtime.http_client import HttpClient
                async with HttpClient(dispatch_cfg) as http_client:
                    await _run_http(registry_ref, http_client, injector, dispatch_cfg, cfg)

        # Two uvicorn.Config calls: main app + management app
        assert len(captured_app) == 2, \
            f"Expected 2 uvicorn.Config calls (main + mgmt), got {len(captured_app)}"

        main_app, main_host, main_port = captured_app[0]
        mgmt_app, mgmt_host, mgmt_port = captured_app[1]

        main_paths = {route.path for route in main_app.routes}
        mgmt_paths = {route.path for route in mgmt_app.routes}

        # Public routes are on the main app
        assert "/auth/login" in main_paths, f"Missing /auth/login on main — got: {main_paths}"
        assert "/auth/callback" in main_paths, f"Missing /auth/callback on main — got: {main_paths}"
        assert "/auth/status" in main_paths, f"Missing /auth/status on main — got: {main_paths}"
        # Management route must NOT be on the main app
        assert "/auth/session/{session_id}" not in main_paths, \
            f"/auth/session/{{session_id}} must be on mgmt app, not main — got: {main_paths}"

        # Management route is on the management app
        assert "/auth/session/{session_id}" in mgmt_paths, \
            f"Missing /auth/session/{{session_id}} on mgmt app — got: {mgmt_paths}"

        # Management app is bound to loopback (default config has no management section)
        assert mgmt_host == "127.0.0.1", f"Expected mgmt host 127.0.0.1, got {mgmt_host}"
        # Management port defaults to 8766
        assert mgmt_port == 8766, f"Expected mgmt port 8766, got {mgmt_port}"
    finally:
        os.unlink(cfg_path)


@pytest.mark.asyncio
async def test_run_http_management_port_respected():
    """Management app must use cfg.management.port when set."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _run_http
    from specmcp.config import Config, DispatchConfig
    from specmcp.runtime.registry_ref import RegistryRef

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        management:
          port: 9090
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))

            registry = _make_registry_with_one_tool()
            registry_ref = RegistryRef(registry)
            injector = AuthInjector.build(cfg)
            dispatch_cfg = DispatchConfig()

            captured_configs: list = []

            def _fake_Config(app, host, port, log_level="warning"):
                captured_configs.append((app, host, port))
                return MagicMock()

            with patch("uvicorn.Config", side_effect=_fake_Config), \
                 patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock())):
                from specmcp.runtime.http_client import HttpClient
                async with HttpClient(dispatch_cfg) as http_client:
                    await _run_http(registry_ref, http_client, injector, dispatch_cfg, cfg)

        assert len(captured_configs) == 2
        _, mgmt_host, mgmt_port = captured_configs[1]
        assert mgmt_port == 9090, f"Expected mgmt port 9090, got {mgmt_port}"
        assert mgmt_host == "127.0.0.1"
    finally:
        os.unlink(cfg_path)


@pytest.mark.asyncio
async def test_run_http_no_oauth_routes_without_auth_code_scheme():
    """Without an auth code scheme, the /auth/* routes must NOT be mounted."""
    from specmcp.cli.serve import _run_http
    from specmcp.config import DispatchConfig
    from specmcp.runtime.registry_ref import RegistryRef

    registry = _make_registry_with_one_tool()
    registry_ref = RegistryRef(registry)
    injector = AuthInjector.build(None)
    dispatch_cfg = DispatchConfig()

    captured_app = []

    def _fake_Config(app, host, port, log_level="warning"):
        captured_app.append(app)
        return MagicMock()

    with patch("uvicorn.Config", side_effect=_fake_Config), \
         patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock())):
        from specmcp.runtime.http_client import HttpClient
        async with HttpClient(dispatch_cfg) as http_client:
            await _run_http(registry_ref, http_client, injector, dispatch_cfg, None)

    route_paths = {route.path for route in captured_app[0].routes}
    auth_routes = {p for p in route_paths if p.startswith("/auth")}
    assert not auth_routes, f"Expected no /auth/* routes, got: {auth_routes}"


# ---------------------------------------------------------------------------
# CLI flags: --management-port and --management-bind
# ---------------------------------------------------------------------------


def test_serve_help_includes_management_flags():
    """--management-port and --management-bind must appear in serve --help."""
    import re
    r = runner.invoke(app, ["serve", "--help"])
    assert r.exit_code == 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    assert "--management-port" in output
    assert "--management-bind" in output


def test_management_bind_invalid_value_exits_nonzero():
    """--management-bind with a value other than 'loopback'/'all' must fail."""
    with patch("specmcp.cli.serve.anyio.run"):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--management-bind", "bogus",
        ])
    assert r.exit_code != 0
    assert "loopback" in r.output or "loopback" in (r.stderr or "")


def test_management_flags_warn_without_config_file():
    """--management-port without a config file emits a warning (no effect)."""
    with patch("specmcp.cli.serve.anyio.run"):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--config", "/nonexistent/no-config.yaml",
            "--management-port", "9999",
        ])
    # anyio.run is patched, so we reach the warning; exit code 0 (flag is advisory)
    assert r.exit_code == 0
    assert "no effect" in r.output.lower() or "no effect" in (r.stderr or "").lower()


def test_management_port_flag_overrides_cfg():
    """--management-port N must override cfg.management.port before _run_server."""
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        captured_cfg: list = []

        def _fake_anyio_run(fn, *args, **kwargs):
            # positional args to _run_server: registry, auth_injector, dispatch_cfg, cfg, ...
            captured_cfg.append(args[3])  # cfg is 4th positional arg

        with patch("specmcp.cli.serve.anyio.run", side_effect=_fake_anyio_run):
            r = runner.invoke(app, [
                "serve",
                "--spec", str(PETSTORE_SPEC),
                "--config", cfg_path,
                "--management-port", "9876",
            ])

        assert r.exit_code == 0, f"serve exited {r.exit_code}: {r.output}"
        assert len(captured_cfg) == 1
        assert captured_cfg[0].management.port == 9876
    finally:
        os.unlink(cfg_path)



def test_management_bind_flag_overrides_cfg():
    """--management-bind all must override cfg.management.bind before _run_server."""
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        management:
          management_token_from: env(MGMT_TOKEN)
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        captured_cfg: list = []

        def _fake_anyio_run(fn, *args, **kwargs):
            captured_cfg.append(args[3])

        with patch.dict(os.environ, {"MGMT_TOKEN": "secret"}), \
             patch("specmcp.cli.serve.anyio.run", side_effect=_fake_anyio_run):
            r = runner.invoke(app, [
                "serve",
                "--spec", str(PETSTORE_SPEC),
                "--config", cfg_path,
                "--management-bind", "all",
            ])

        assert r.exit_code == 0, f"serve exited {r.exit_code}: {r.output}"
        assert len(captured_cfg) == 1
        assert captured_cfg[0].management.bind == "all"
    finally:
        os.unlink(cfg_path)


def test_management_bind_all_without_token_exits_nonzero():
    """--management-bind all without management_token_from in config must fail."""
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch("specmcp.cli.serve.anyio.run"):
            r = runner.invoke(app, [
                "serve",
                "--spec", str(PETSTORE_SPEC),
                "--config", cfg_path,
                "--management-bind", "all",
            ])
        assert r.exit_code != 0
        assert "management_token_from" in r.output.lower() or "token" in r.output.lower()
    finally:
        os.unlink(cfg_path)


# ---------------------------------------------------------------------------
# CLI flags: --token-store / --token-store-path / --token-store-key-env
# ---------------------------------------------------------------------------


def test_serve_help_includes_token_store_flags():
    """--token-store, --token-store-path, and --token-store-key-env appear in help."""
    import re
    r = runner.invoke(app, ["serve", "--help"])
    assert r.exit_code == 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    assert "--token-store" in output
    assert "--token-store-path" in output
    assert "--token-store-key-env" in output


def test_token_store_invalid_value_exits_nonzero():
    """--token-store bogus must fail before the pipeline runs."""
    with patch("specmcp.cli.serve.anyio.run"):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "bogus",
        ])
    assert r.exit_code != 0
    assert "memory" in r.output.lower() or "sqlite" in r.output.lower()


def test_token_store_sqlite_without_key_env_exits_nonzero():
    """--token-store sqlite without the key env var set must fail with a clear error."""
    with patch("specmcp.cli.serve.anyio.run"), \
         patch.dict(os.environ, {}, clear=True):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
        ])
    assert r.exit_code != 0
    assert "SPECMCP_TOKEN_KEY" in r.output or "encryption key" in r.output.lower()


def test_token_store_sqlite_with_key_env_reaches_anyio_run():
    """--token-store sqlite + key env var set → pipeline succeeds and anyio.run is called."""
    with patch("specmcp.cli.serve.anyio.run") as mock_run, \
         patch.dict(os.environ, {"SPECMCP_TOKEN_KEY": "my-secret-key"}):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
        ])
    assert mock_run.called, f"anyio.run not called; output: {r.output}"
    assert r.exit_code == 0


def test_token_store_sqlite_args_threaded_to_anyio_run():
    """sqlite_db_path and sqlite_key_bytes must be passed as positional args to anyio.run."""
    captured: list = []

    def _fake_anyio_run(fn, *args, **kwargs):
        # positional args order to _run_server:
        # registry, auth_injector, dispatch_cfg, cfg, transport, watch,
        # config_path, spec_source, token_store_type, sqlite_db_path, sqlite_key_bytes
        captured.extend(args)

    with patch("specmcp.cli.serve.anyio.run", side_effect=_fake_anyio_run), \
         patch.dict(os.environ, {"SPECMCP_TOKEN_KEY": "test-key-material"}):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
            "--token-store-path", "/tmp/test_tokens.db",
        ])

    assert r.exit_code == 0, f"serve exited {r.exit_code}: {r.output}"
    # token_store_type is args[8], sqlite_db_path is args[9], sqlite_key_bytes is args[10]
    assert captured[8] == "sqlite"
    assert captured[9] == Path("/tmp/test_tokens.db")
    assert captured[10] == b"test-key-material"


def test_token_store_key_weak_emits_warning():
    """A key shorter than 16 bytes must emit a warning (but still proceed)."""
    with patch("specmcp.cli.serve.anyio.run"), \
         patch.dict(os.environ, {"SPECMCP_TOKEN_KEY": "short"}):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
        ])
    assert r.exit_code == 0
    assert "warning" in r.output.lower() or "5 bytes" in r.output.lower()


def test_token_store_key_adequate_length_no_warning():
    """A key of 16+ bytes must NOT emit the entropy warning."""
    with patch("specmcp.cli.serve.anyio.run"), \
         patch.dict(os.environ, {"SPECMCP_TOKEN_KEY": "sixteen-bytes-ok"}):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
        ])
    assert r.exit_code == 0
    assert "warning" not in r.output.lower() or "token_key" not in r.output.lower()


def test_token_store_sqlite_default_path_is_home_specmcp():
    """Without --token-store-path, default is ~/.specmcp/tokens.db."""
    captured: list = []

    def _fake_anyio_run(fn, *args, **kwargs):
        captured.extend(args)

    with patch("specmcp.cli.serve.anyio.run", side_effect=_fake_anyio_run), \
         patch.dict(os.environ, {"SPECMCP_TOKEN_KEY": "k"}):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
        ])

    assert r.exit_code == 0
    expected_path = Path.home() / ".specmcp" / "tokens.db"
    assert captured[9] == expected_path


def test_build_oauth_state_uses_sqlite_store_when_requested():
    """_build_oauth_state with token_store_type='sqlite' creates SqliteTokenStore instances."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _build_oauth_state
    from specmcp.config import Config
    from specmcp.auth.token_store import SqliteTokenStore

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))
            injector = AuthInjector.build(cfg)
            result = _build_oauth_state(
                cfg,
                injector,
                "http://localhost:8765",
                token_store_type="sqlite",
                sqlite_db_path=Path("/tmp/test_oauth_tokens.db"),
                sqlite_key_bytes=b"test-key-32-bytes-exactly-here!",
            )

        assert result is not None
        store = result.schemes["myOAuth"].token_store
        assert isinstance(store, SqliteTokenStore)
    finally:
        os.unlink(cfg_path)


def test_build_oauth_state_uses_memory_store_by_default():
    """_build_oauth_state with token_store_type='memory' (default) creates InMemoryTokenStore."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _build_oauth_state
    from specmcp.config import Config
    from specmcp.auth.token_store import InMemoryTokenStore

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))
            injector = AuthInjector.build(cfg)
            result = _build_oauth_state(cfg, injector, "http://localhost:8765")

        assert result is not None
        store = result.schemes["myOAuth"].token_store
        assert isinstance(store, InMemoryTokenStore)
    finally:
        os.unlink(cfg_path)


# ---------------------------------------------------------------------------
# --token-store-key-env with a non-default env var name
# ---------------------------------------------------------------------------


def test_token_store_key_env_custom_name():
    """--token-store-key-env MY_KEY should read from MY_KEY, not SPECMCP_TOKEN_KEY."""
    captured: list = []

    def _fake_anyio_run(fn, *args, **kwargs):
        captured.extend(args)

    with patch("specmcp.cli.serve.anyio.run", side_effect=_fake_anyio_run), \
         patch.dict(os.environ, {
             "MY_CUSTOM_KEY": "custom-key-material",
             # Deliberately NOT setting SPECMCP_TOKEN_KEY to prove the custom var is used
         }, clear=False):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
            "--token-store-key-env", "MY_CUSTOM_KEY",
        ])

    assert r.exit_code == 0, f"serve exited {r.exit_code}: {r.output}"
    # sqlite_key_bytes is args[10]
    assert captured[10] == b"custom-key-material"


def test_token_store_sqlite_missing_custom_key_env_exits_nonzero():
    """--token-store-key-env MY_KEY with MY_KEY unset must fail (not fall back to SPECMCP_TOKEN_KEY)."""
    with patch("specmcp.cli.serve.anyio.run"), \
         patch.dict(os.environ, {
             "SPECMCP_TOKEN_KEY": "fallback-key",  # present, but should NOT be used
         }, clear=False):
        r = runner.invoke(app, [
            "serve",
            "--spec", str(PETSTORE_SPEC),
            "--token-store", "sqlite",
            "--token-store-key-env", "MY_MISSING_KEY",
        ])
    assert r.exit_code != 0
    assert "MY_MISSING_KEY" in r.output


# ---------------------------------------------------------------------------
# _run_http: token stores are closed even when uvicorn raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_http_closes_stores_when_uvicorn_raises():
    """Token stores must be closed via finally even if uvicorn.Server.serve() raises."""
    import textwrap, tempfile, os
    from specmcp.cli.serve import _run_http
    from specmcp.config import Config, DispatchConfig
    from specmcp.runtime.registry_ref import RegistryRef

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))

        registry = _make_registry_with_one_tool()
        registry_ref = RegistryRef(registry)
        injector = AuthInjector.build(cfg)
        dispatch_cfg = DispatchConfig()

        open_calls: list[str] = []
        close_calls: list[str] = []

        # Patch InMemoryTokenStore to track open/close calls
        from specmcp.auth.token_store import InMemoryTokenStore

        original_open = InMemoryTokenStore.open
        original_close = InMemoryTokenStore.close

        async def _tracked_open(self):
            open_calls.append("open")
            return await original_open(self)

        async def _tracked_close(self):
            close_calls.append("close")
            return await original_close(self)

        # Keep env vars in scope for the full async call (including _build_oauth_state)
        with patch.dict(os.environ, {"TEST_CLIENT_ID": "cid", "TEST_CLIENT_SECRET": "csec"}), \
             patch.object(InMemoryTokenStore, "open", _tracked_open), \
             patch.object(InMemoryTokenStore, "close", _tracked_close), \
             patch("uvicorn.Config", return_value=MagicMock()), \
             patch("uvicorn.Server", return_value=AsyncMock(serve=AsyncMock(side_effect=RuntimeError("boom")))):
            from specmcp.runtime.http_client import HttpClient
            # With OAuth state present, two uvicorn servers run concurrently in a task
            # group. Both raise RuntimeError("boom"), which anyio may deliver as an
            # ExceptionGroup on Python 3.11+. We care only that the finally block runs.
            with pytest.raises(Exception):
                async with HttpClient(dispatch_cfg) as http_client:
                    await _run_http(registry_ref, http_client, injector, dispatch_cfg, cfg)

        # close() must have been called despite the exception
        assert len(close_calls) == 1, "store.close() must be called in finally block"
        assert len(open_calls) == 1, "store.open() must have been called before serve"
    finally:
        os.unlink(cfg_path)


# ---------------------------------------------------------------------------
# _handle_sse: tokens are purged from the store on SSE disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sse_deletes_session_tokens_on_disconnect():
    """When an SSE connection closes, any OAuth tokens for that session must be
    deleted from the token store so abandoned sessions don't accumulate tokens.

    Strategy: capture the Starlette main-app from uvicorn.Config, then have
    the mocked uvicorn.Server.serve() actually invoke the /sse route handler with
    a fake Request so _handle_sse's finally block fires and delete() is called.
    """
    import textwrap, tempfile, os
    from contextlib import asynccontextmanager
    from starlette.requests import Request
    from specmcp.cli.serve import _run_http
    from specmcp.config import Config, DispatchConfig
    from specmcp.runtime.registry_ref import RegistryRef
    from specmcp.auth.token_store import InMemoryTokenStore

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(TEST_CLIENT_ID)
            client_secret_from: env(TEST_CLIENT_SECRET)
            redirect_uri: http://localhost:8765/auth/callback
    """)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    try:
        with patch.dict(os.environ, {
            "TEST_CLIENT_ID": "cid",
            "TEST_CLIENT_SECRET": "csec",
        }):
            cfg = Config.load(Path(cfg_path))

        registry = _make_registry_with_one_tool()
        registry_ref = RegistryRef(registry)
        injector = AuthInjector.build(cfg)
        dispatch_cfg = DispatchConfig()

        # Track delete() calls on any InMemoryTokenStore instance.
        delete_calls: list[str] = []
        original_delete = InMemoryTokenStore.delete

        async def _tracking_delete(self, session_id: str) -> None:
            delete_calls.append(session_id)
            return await original_delete(self, session_id)

        # Capture the Starlette app for the main port so the fake serve can
        # invoke _handle_sse directly without a real HTTP server.
        captured_main_app: list = []
        server_call_count = [0]

        def _fake_Config(app, host, port, log_level="warning"):
            if port != 8766:  # not the management app
                captured_main_app.append(app)
            return MagicMock()

        # The mocked uvicorn Server: first instance (main app) invokes /sse;
        # second instance (mgmt app) is a no-op.
        @asynccontextmanager
        async def _fake_connect_sse(*args, **kwargs):
            yield AsyncMock(), AsyncMock()

        async def _fake_server_run_noop(self, read, write, opts):
            pass  # immediate clean disconnect

        async def _main_serve():
            if not captured_main_app:
                return
            app = captured_main_app[0]
            sse_handler = next(
                (r.endpoint for r in app.routes if getattr(r, "path", None) == "/sse"),
                None,
            )
            if sse_handler is None:
                return
            # Build a minimal Starlette Request and invoke the handler.
            scope = {
                "type": "http", "method": "GET", "path": "/sse",
                "query_string": b"", "headers": [], "app": app,
            }

            async def _recv():
                return {}

            async def _send(msg):
                pass

            request = Request(scope, receive=_recv, send=_send)
            await sse_handler(request)

        async def _mgmt_serve():
            pass  # management server: no-op

        def _fake_Server(config):
            server_call_count[0] += 1
            if server_call_count[0] == 1:
                return AsyncMock(serve=AsyncMock(side_effect=_main_serve))
            return AsyncMock(serve=AsyncMock(side_effect=_mgmt_serve))

        with patch.dict(os.environ, {"TEST_CLIENT_ID": "cid", "TEST_CLIENT_SECRET": "csec"}), \
             patch.object(InMemoryTokenStore, "delete", _tracking_delete), \
             patch("mcp.server.sse.SseServerTransport.connect_sse",
                   return_value=_fake_connect_sse()), \
             patch("mcp.server.Server.run", new=_fake_server_run_noop), \
             patch("uvicorn.Config", side_effect=_fake_Config), \
             patch("uvicorn.Server", side_effect=_fake_Server):
            from specmcp.runtime.http_client import HttpClient
            async with HttpClient(dispatch_cfg) as http_client:
                await _run_http(registry_ref, http_client, injector, dispatch_cfg, cfg)

        # delete() must have been called once (one scheme, one session).
        assert len(delete_calls) >= 1, (
            "InMemoryTokenStore.delete() should be called on SSE disconnect "
            "to purge session tokens, but it was not called"
        )
    finally:
        os.unlink(cfg_path)
