"""
End-to-end integration test: serve pipeline → MCP tools/list + tools/call.

Uses anyio memory streams to connect the specmcp Server and an MCP
ClientSession in-process — no subprocess, no real network.

Upstream HTTP calls are intercepted by respx so the test is fully
hermetic (no real petstore.example.com requests).

Test matrix:
  - tools/list returns the 4 petstore tools
  - tools/call getPetById → upstream mocked → MCP text content returned
  - tools/call with missing tool → error text returned
  - tools/call upstream 4xx → UpstreamClientError → MCP error text
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import respx

import mcp.types as mcp_types
from mcp.client.session import ClientSession
from mcp.server import Server

PETSTORE_SPEC = Path(__file__).parent.parent.parent / "test-corpus" / "petstore.json"
PETSTORE_BASE = "https://petstore.example.com/v1"


# ---------------------------------------------------------------------------
# Test infrastructure: build the specmcp MCP Server in-process
# ---------------------------------------------------------------------------


def _build_server() -> tuple[Server, Any, Any]:
    """Run the full specmcp pipeline and return (mcp_server, registry, auth_injector)."""
    from specmcp.auth.injector import AuthInjector, ResolvedScheme
    from specmcp.config import ApiKeyAuthConfig, SimplifyConfig, SensitiveStr
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    _, resolved = load_spec(str(PETSTORE_SPEC))
    ops = normalize(resolved, base_url_override=None, include_deprecated=True,
                    include_tags=None, exclude_tags=None,
                    include_operations=None, exclude_operations=None)
    simplified_ops = simplify(ops, config=SimplifyConfig())
    registry = ToolRegistry.build(simplified_ops, config=None)

    # Petstore requires petstoreApiKey — configure a dummy credential for tests.
    # respx intercepts before the network so the value doesn't matter.
    petstore_key_cfg = ApiKeyAuthConfig.model_validate({
        "type": "apiKey", "in": "header", "name": "X-Api-Key",
        "value_from": "env(PETSTORE_API_KEY)",
    })
    auth_injector = AuthInjector(_schemes={
        "petstoreApiKey": ResolvedScheme(
            scheme_name="petstoreApiKey",
            config=petstore_key_cfg,
            credential=SensitiveStr("test-key"),
        )
    })

    server = Server("specmcp-test")

    @server.list_tools()
    async def handle_list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
            )
            for t in registry.tools
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        from specmcp.config import DispatchConfig
        from specmcp.errors import SpecmcpError, mcp_error_content
        from specmcp.runtime.dispatcher import dispatch
        from specmcp.runtime.http_client import HttpClient

        tool = registry.lookup(name)
        if tool is None:
            return [mcp_types.TextContent(type="text", text=f"Unknown tool: {name!r}")]

        dispatch_cfg = DispatchConfig(tls_verify=False)
        async with HttpClient(dispatch_cfg) as http_client:
            try:
                blocks = await dispatch(
                    tool=tool,
                    llm_args=arguments or {},
                    http_client=http_client,
                    auth_injector=auth_injector,
                    dispatch_config=dispatch_cfg,
                )
                return [
                    mcp_types.TextContent(type="text", text=b["text"])
                    for b in blocks if b.get("type") == "text"
                ]
            except SpecmcpError as exc:
                return [mcp_types.TextContent(type="text", text=mcp_error_content(exc))]

    return server, registry, auth_injector


async def _run_client_session(server: Server, test_fn):
    """Wire server + client via in-process memory streams and run test_fn."""
    # Four streams: server reads from s_rx, writes to s_tx;
    #               client reads from c_rx, writes to c_tx.
    s_tx, c_rx = anyio.create_memory_object_stream(max_buffer_size=100)
    c_tx, s_rx = anyio.create_memory_object_stream(max_buffer_size=100)

    init_options = server.create_initialization_options()

    async def run_server():
        await server.run(s_rx, s_tx, init_options)

    async def run_client():
        async with ClientSession(c_rx, c_tx) as session:
            await session.initialize()
            await test_fn(session)
        # Signal server to stop
        await s_rx.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_server)
        tg.start_soon(run_client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_e2e_tools_list_returns_four_tools():
    server, _, _ = _build_server()

    results = []

    async def check(session: ClientSession):
        resp = await session.list_tools()
        results.extend(resp.tools)

    await _run_client_session(server, check)

    assert len(results) == 4
    names = {t.name for t in results}
    assert names == {"listPets", "createPet", "getPetById", "deletePet"}


@pytest.mark.asyncio
@respx.mock
async def test_e2e_tools_list_schema_matches_design_doc():
    """getPetById inputSchema must match design doc §5.5."""
    server, _, _ = _build_server()
    found = []

    async def check(session: ClientSession):
        resp = await session.list_tools()
        for t in resp.tools:
            if t.name == "getPetById":
                found.append(t)

    await _run_client_session(server, check)

    assert len(found) == 1
    t = found[0]
    assert t.description == "Get a pet by ID [GET /pets/{petId}]"
    schema = t.inputSchema
    assert schema["type"] == "object"
    assert "petId" in schema["required"]
    assert schema["properties"]["petId"]["type"] == "integer"
    assert "verbose" in schema["properties"]


@pytest.mark.asyncio
@respx.mock
async def test_e2e_call_get_pet_by_id_success():
    """tools/call getPetById → mocked upstream → MCP text content."""
    respx.get(f"{PETSTORE_BASE}/pets/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "Rex"})
    )

    server, _, _ = _build_server()
    results = []

    async def check(session: ClientSession):
        resp = await session.call_tool("getPetById", {"petId": 42})
        results.extend(resp.content)

    await _run_client_session(server, check)

    assert len(results) >= 1
    text_block = next(b for b in results if hasattr(b, "text"))
    payload = json.loads(text_block.text)
    assert payload["id"] == 42
    assert payload["name"] == "Rex"


@pytest.mark.asyncio
@respx.mock
async def test_e2e_call_list_pets_with_limit():
    """tools/call listPets?limit=5 → limit appears in upstream query string."""
    route = respx.get(f"{PETSTORE_BASE}/pets").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Fido"}])
    )

    server, _, _ = _build_server()
    results = []

    async def check(session: ClientSession):
        resp = await session.call_tool("listPets", {"limit": 5})
        results.extend(resp.content)

    await _run_client_session(server, check)

    assert route.called
    # Verify the limit query param was forwarded
    request = route.calls.last.request
    assert "limit=5" in str(request.url)


@pytest.mark.asyncio
@respx.mock
async def test_e2e_call_create_pet_sends_body():
    """tools/call createPet → name+tag in JSON body to upstream."""
    route = respx.post(f"{PETSTORE_BASE}/pets").mock(
        return_value=httpx.Response(201, json={"id": 99, "name": "Bella"})
    )

    server, _, _ = _build_server()

    async def check(session: ClientSession):
        await session.call_tool("createPet", {"name": "Bella", "tag": "dog"})

    await _run_client_session(server, check)

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["name"] == "Bella"
    assert sent_body.get("tag") == "dog"


@pytest.mark.asyncio
@respx.mock
async def test_e2e_upstream_4xx_returns_error_text():
    """Upstream 404 → UpstreamClientError → MCP text block with status code."""
    respx.get(f"{PETSTORE_BASE}/pets/999").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    server, _, _ = _build_server()
    results = []

    async def check(session: ClientSession):
        resp = await session.call_tool("getPetById", {"petId": 999})
        results.extend(resp.content)

    await _run_client_session(server, check)

    text_block = next(b for b in results if hasattr(b, "text"))
    assert "404" in text_block.text


@pytest.mark.asyncio
@respx.mock
async def test_e2e_unknown_tool_returns_error_text():
    """Calling a non-existent tool returns a text error, not a crash."""
    server, _, _ = _build_server()
    results = []

    async def check(session: ClientSession):
        resp = await session.call_tool("nonExistentTool", {})
        results.extend(resp.content)

    await _run_client_session(server, check)

    text_block = next(b for b in results if hasattr(b, "text"))
    assert "nonExistentTool" in text_block.text
