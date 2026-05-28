"""Unit tests for specmcp.runtime.dispatcher."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from specmcp.auth.injector import AuthInjector
from specmcp.config import DispatchConfig
from specmcp.core.expose import ToolDefinition
from specmcp.core.model import (
    ArgumentBinding,
    ArgumentMap,
    AuthRequirement,
    Operation,
    Parameter,
    Response,
    SimplifiedOperation,
)
from specmcp.errors import DispatchError
from specmcp.runtime.dispatcher import (
    _serialize_path,
    _serialize_query,
    _serialize_simple,
    _set_nested,
    dispatch,
)
from specmcp.runtime.http_client import HttpResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dispatch_cfg() -> DispatchConfig:
    return DispatchConfig(
        default_timeout_seconds=5.0,
        per_host_concurrency=5,
        global_concurrency=10,
        response_size_limit_bytes=1_048_576,
        text_truncate_bytes=262_144,
        tls_verify=False,
    )


def _make_op(
    method: str = "GET",
    path: str = "/pets/{petId}",
    server_url: str = "https://api.example.com/v1",
    auth: list | None = None,
) -> Operation:
    return Operation(
        id="getPetById",
        method=method,
        path=path,
        server_url=server_url,
        parameters=[
            Parameter(
                name="petId", location="path", required=True,
                schema_={"type": "integer"},
                style="simple", explode=False,
            )
        ],
        responses=[Response(status_code="200", description="ok")],
        auth=auth or [],
        summary="Get pet",
    )


def _make_tool(op: Operation, arg_map: ArgumentMap) -> ToolDefinition:
    sop = SimplifiedOperation(
        operation=op,
        llm_input_schema={"type": "object", "properties": {}, "required": []},
        llm_description="Get a pet",
        arg_map=arg_map,
        warnings=[],
    )
    return ToolDefinition(
        name=op.id,
        description=sop.llm_description,
        input_schema=sop.llm_input_schema,
        simplified_operation=sop,
    )


def _fake_http_response(body: str = '{"id": 1}', status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        body=body,
        truncated=False,
    )


def _mock_http_client(response: HttpResponse) -> MagicMock:
    client = MagicMock()
    client.request = AsyncMock(return_value=response)
    return client


def _no_auth_injector() -> AuthInjector:
    return AuthInjector.build(None)


# ---------------------------------------------------------------------------
# Path parameter dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_path_param_fills_template():
    op = _make_op()
    arg_map = ArgumentMap(bindings={
        "petId": ArgumentBinding(
            source_llm_key="petId",
            target_kind="path",
            target_path=["petId"],
            style="simple",
            explode=False,
        )
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response())

    await dispatch(
        tool=tool,
        llm_args={"petId": 42},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    client.request.assert_called_once()
    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs["url"] == "https://api.example.com/v1/pets/42"
    assert call_kwargs["method"] == "GET"


@pytest.mark.asyncio
async def test_dispatch_path_param_url_encoded():
    op = _make_op(path="/pets/{name}")
    arg_map = ArgumentMap(bindings={
        "name": ArgumentBinding(
            source_llm_key="name",
            target_kind="path",
            target_path=["name"],
            style="simple",
            explode=False,
        )
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response())

    await dispatch(
        tool=tool,
        llm_args={"name": "fluffy cat"},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert "fluffy%20cat" in call_kwargs["url"]


# ---------------------------------------------------------------------------
# Query parameter dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_query_param_passed():
    op = _make_op(path="/pets", method="GET")
    arg_map = ArgumentMap(bindings={
        "limit": ArgumentBinding(
            source_llm_key="limit",
            target_kind="query",
            target_path=["limit"],
            style="form",
            explode=True,
        )
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"items": []}'))

    await dispatch(
        tool=tool,
        llm_args={"limit": 10},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs["params"].get("limit") == "10"


@pytest.mark.asyncio
async def test_dispatch_missing_optional_query_param_omitted():
    op = _make_op(path="/pets", method="GET")
    arg_map = ArgumentMap(bindings={
        "limit": ArgumentBinding(
            source_llm_key="limit",
            target_kind="query",
            target_path=["limit"],
        )
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"items": []}'))

    await dispatch(
        tool=tool,
        llm_args={},  # limit not provided
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert "limit" not in call_kwargs["params"]


# ---------------------------------------------------------------------------
# Body dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_body_fields_sent_as_json():
    from specmcp.core.model import RequestBody, RequestBodyVariant
    op_base = _make_op(method="POST", path="/pets")
    op = op_base.model_copy(update={
        "request_body": RequestBody(
            required=True,
            variants=[RequestBodyVariant(schema_={"type": "object"}, **{"content_type": "application/json"})],
        )
    })
    arg_map = ArgumentMap(bindings={
        "name": ArgumentBinding(source_llm_key="name", target_kind="body_field", target_path=["name"]),
        "tag": ArgumentBinding(source_llm_key="tag", target_kind="body_field", target_path=["tag"]),
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"id": 99}', status=201))

    await dispatch(
        tool=tool,
        llm_args={"name": "Rex", "tag": "dog"},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs["json_body"] == {"name": "Rex", "tag": "dog"}


@pytest.mark.asyncio
async def test_dispatch_body_root_sent_directly():
    op = _make_op(method="POST", path="/raw")
    arg_map = ArgumentMap(bindings={
        "body": ArgumentBinding(source_llm_key="body", target_kind="body_root", target_path=[]),
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"ok": true}'))

    await dispatch(
        tool=tool,
        llm_args={"body": {"arbitrary": "payload"}},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs["json_body"] == {"arbitrary": "payload"}


# ---------------------------------------------------------------------------
# Auth injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_auth_injected_into_headers():
    from specmcp.auth.injector import ResolvedScheme
    from specmcp.config import ApiKeyAuthConfig, SensitiveStr

    op = _make_op(auth=[[AuthRequirement(scheme_name="myKey")]])
    arg_map = ArgumentMap(bindings={
        "petId": ArgumentBinding(
            source_llm_key="petId", target_kind="path", target_path=["petId"]
        )
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response())

    # Build injector with the key configured
    cfg_obj = ApiKeyAuthConfig.model_validate({
        "type": "apiKey", "in": "header", "name": "X-Api-Key", "value_from": "env(X)"
    })
    injector = AuthInjector(_schemes={
        "myKey": ResolvedScheme("myKey", cfg_obj, SensitiveStr("secret-token"))
    })

    await dispatch(
        tool=tool,
        llm_args={"petId": 1},
        http_client=client,
        auth_injector=injector,
        dispatch_config=_dispatch_cfg(),
    )

    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs["headers"].get("X-Api-Key") == "secret-token"


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_mcp_text_block():
    op = _make_op()
    arg_map = ArgumentMap(bindings={
        "petId": ArgumentBinding(source_llm_key="petId", target_kind="path", target_path=["petId"])
    })
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"id": 1, "name": "Rex"}'))

    result = await dispatch(
        tool=tool,
        llm_args={"petId": 1},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    # JSON should be pretty-printed
    parsed = json.loads(result[0]["text"])
    assert parsed == {"id": 1, "name": "Rex"}


@pytest.mark.asyncio
async def test_dispatch_truncated_response_appends_note():
    op = _make_op()
    arg_map = ArgumentMap(bindings={
        "petId": ArgumentBinding(source_llm_key="petId", target_kind="path", target_path=["petId"])
    })
    tool = _make_tool(op, arg_map)

    truncated_resp = HttpResponse(
        status_code=200, headers={}, body="partial content", truncated=True
    )
    client = _mock_http_client(truncated_resp)

    result = await dispatch(
        tool=tool,
        llm_args={"petId": 1},
        http_client=client,
        auth_injector=_no_auth_injector(),
        dispatch_config=_dispatch_cfg(),
    )

    assert "[Response truncated]" in result[0]["text"]


# ---------------------------------------------------------------------------
# Serialisation unit tests
# ---------------------------------------------------------------------------


class _Binding:
    """Lightweight mock for ArgumentBinding."""
    def __init__(self, style=None, explode=None, target_path=None):
        self.style = style
        self.explode = explode
        self.target_path = target_path or ["param"]


def test_serialize_path_simple_scalar():
    b = _Binding(style="simple", explode=False)
    assert _serialize_path("hello world", b) == "hello%20world"


def test_serialize_path_simple_list():
    b = _Binding(style="simple", explode=False)
    assert _serialize_path(["a", "b", "c"], b) == "a,b,c"


def test_serialize_path_label_scalar():
    b = _Binding(style="label", explode=False)
    assert _serialize_path("value", b) == ".value"


def test_serialize_path_matrix_scalar():
    b = _Binding(style="matrix", explode=False, target_path=["color"])
    assert _serialize_path("blue", b) == ";color=blue"


def test_serialize_query_form_scalar():
    b = _Binding(style="form", explode=True, target_path=["status"])
    result = _serialize_query("status", "active", b)
    assert result == {"status": "active"}


def test_serialize_query_form_dict_explode():
    b = _Binding(style="form", explode=True, target_path=["filter"])
    result = _serialize_query("filter", {"color": "blue", "size": "M"}, b)
    assert result.get("color") == "blue"
    assert result.get("size") == "M"


def test_serialize_query_spaceDelimited_list():
    b = _Binding(style="spaceDelimited", explode=False, target_path=["ids"])
    result = _serialize_query("ids", [1, 2, 3], b)
    assert result == {"ids": "1 2 3"}


def test_serialize_query_pipeDelimited_list():
    b = _Binding(style="pipeDelimited", explode=False, target_path=["ids"])
    result = _serialize_query("ids", [1, 2, 3], b)
    assert result == {"ids": "1|2|3"}


def test_serialize_query_deepObject():
    b = _Binding(style="deepObject", explode=True, target_path=["filter"])
    result = _serialize_query("filter", {"color": "blue"}, b)
    assert result == {"filter[color]": "blue"}


def test_serialize_simple_scalar():
    assert _serialize_simple(42) == "42"


def test_serialize_simple_list():
    assert _serialize_simple(["a", "b"]) == "a,b"


def test_set_nested_single_key():
    obj: dict = {}
    _set_nested(obj, ["name"], "Rex")
    assert obj == {"name": "Rex"}


def test_set_nested_deep_path():
    obj: dict = {}
    _set_nested(obj, ["data", "pet", "name"], "Rex")
    assert obj == {"data": {"pet": {"name": "Rex"}}}


def test_set_nested_overwrites_scalar_with_dict():
    obj: dict = {"data": "old"}
    _set_nested(obj, ["data", "key"], "new")
    assert obj == {"data": {"key": "new"}}


# ---------------------------------------------------------------------------
# Input validation (defence-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_invalid_args_raises_argument_validation_error():
    """Args that violate inputSchema → ArgumentValidationError before HTTP call."""
    from specmcp.errors import ArgumentValidationError

    op = _make_op()
    # Schema requires petId: integer
    arg_map = ArgumentMap(bindings={
        "petId": ArgumentBinding(
            source_llm_key="petId", target_kind="path", target_path=["petId"]
        )
    })
    # Override input schema to make petId required with type integer
    sop = SimplifiedOperation(
        operation=op,
        llm_input_schema={
            "type": "object",
            "required": ["petId"],
            "properties": {"petId": {"type": "integer"}},
        },
        llm_description="Get a pet",
        arg_map=arg_map,
        warnings=[],
    )
    tool = ToolDefinition(
        name=op.id,
        description=sop.llm_description,
        input_schema=sop.llm_input_schema,
        simplified_operation=sop,
    )
    client = _mock_http_client(_fake_http_response())

    with pytest.raises(ArgumentValidationError):
        await dispatch(
            tool=tool,
            llm_args={"petId": "not-an-integer"},   # string instead of integer
            http_client=client,
            auth_injector=_no_auth_injector(),
            dispatch_config=_dispatch_cfg(),
        )

    # HTTP client must not have been called
    client.request.assert_not_called()


# ---------------------------------------------------------------------------
# Session passthrough — dispatcher forwards session to auth injector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_passes_session_to_injector():
    """dispatch() must forward the session kwarg to auth_injector.inject()."""
    from specmcp.config import BearerAuthConfig, SensitiveStr
    from specmcp.auth.injector import ResolvedScheme
    from specmcp.runtime.session import SessionContext

    op = _make_op(
        method="GET",
        path="/items",
        auth=[[AuthRequirement(scheme_name="myBearer")]],
    )
    arg_map = ArgumentMap(bindings={})
    tool = _make_tool(op, arg_map)
    client = _mock_http_client(_fake_http_response('{"ok": true}'))

    # Build an injector with a static bearer token
    cfg = BearerAuthConfig(type="bearer", value_from="env(X)")
    resolved = ResolvedScheme(
        scheme_name="myBearer",
        config=cfg,
        credential=SensitiveStr("static-token"),
    )
    injector = AuthInjector(_schemes={"myBearer": resolved}, _token_caches={})

    # Session with a client_token — should override static token
    session = SessionContext(session_id="sid", client_token=SensitiveStr("session-token"))

    captured_headers: dict = {}

    async def _fake_request(**kwargs: Any) -> HttpResponse:
        captured_headers.update(kwargs.get("headers", {}))
        return _fake_http_response('{"ok": true}')

    client.request = AsyncMock(side_effect=_fake_request)

    await dispatch(
        tool=tool,
        llm_args={},
        http_client=client,
        auth_injector=injector,
        dispatch_config=_dispatch_cfg(),
        session=session,
    )

    # client_token takes priority over static token
    assert captured_headers.get("Authorization") == "Bearer session-token"


@pytest.mark.asyncio
async def test_dispatch_session_none_uses_static_token():
    """dispatch() with session=None falls back to static bearer credential."""
    from specmcp.config import BearerAuthConfig, SensitiveStr
    from specmcp.auth.injector import ResolvedScheme

    op = _make_op(
        method="GET",
        path="/items",
        auth=[[AuthRequirement(scheme_name="myBearer")]],
    )
    arg_map = ArgumentMap(bindings={})
    tool = _make_tool(op, arg_map)

    cfg = BearerAuthConfig(type="bearer", value_from="env(X)")
    resolved = ResolvedScheme(
        scheme_name="myBearer",
        config=cfg,
        credential=SensitiveStr("env-token"),
    )
    injector = AuthInjector(_schemes={"myBearer": resolved}, _token_caches={})

    captured_headers: dict = {}

    async def _fake_request(**kwargs: Any) -> HttpResponse:
        captured_headers.update(kwargs.get("headers", {}))
        return _fake_http_response('{"ok": true}')

    client = _mock_http_client(_fake_http_response())
    client.request = AsyncMock(side_effect=_fake_request)

    await dispatch(
        tool=tool,
        llm_args={},
        http_client=client,
        auth_injector=injector,
        dispatch_config=_dispatch_cfg(),
        session=None,  # explicitly no session
    )

    assert captured_headers.get("Authorization") == "Bearer env-token"


# ---------------------------------------------------------------------------
# 401 retry — OAuth client_credentials token invalidation
# ---------------------------------------------------------------------------


def _make_oauth_tool() -> tuple[ToolDefinition, AuthInjector]:
    """Return a tool and injector wired with an oauth2_client_credentials scheme."""
    import os
    import textwrap
    from specmcp.config import Config

    op = _make_op(
        method="GET",
        path="/items",
        auth=[[AuthRequirement(scheme_name="myOAuth")]],
    )
    arg_map = ArgumentMap(bindings={})
    tool = _make_tool(op, arg_map)

    yaml_cfg = textwrap.dedent("""
        version: "1"
        spec:
          source: https://example.com/openapi.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: https://auth.example.com/token
            client_id_from: env(OAUTH_CLIENT_ID)
            client_secret_from: env(OAUTH_CLIENT_SECRET)
    """)
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        os.environ,
        {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
    ):
        cfg = Config.model_validate(__import__("yaml").safe_load(yaml_cfg))
        injector = AuthInjector.build(cfg)

    return tool, injector


@pytest.mark.asyncio
async def test_dispatch_retries_on_401_with_oauth_token():
    """A 401 from upstream invalidates the cached token and retries once."""
    import os
    from specmcp.errors import UpstreamClientError

    tool, injector = _make_oauth_tool()

    # Pre-seed a token so the first inject() doesn't need to call the endpoint
    from specmcp.auth.token_cache import CachedToken
    from specmcp.config import SensitiveStr
    import time

    injector._token_caches["myOAuth"]._token = CachedToken(
        access_token=SensitiveStr("stale-token"),
        expires_at=time.monotonic() + 3600,
    )

    client = MagicMock()
    call_count = 0

    import respx, httpx

    async def _side_effect(**kwargs: Any) -> HttpResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise UpstreamClientError(
                "Unauthorized", status_code=401, body="token expired"
            )
        # Second call: inject will have fetched a fresh token via the mock endpoint
        return _fake_http_response('{"ok": true}')

    client.request = AsyncMock(side_effect=_side_effect)

    with respx.mock:
        respx.post("https://auth.example.com/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "fresh-token", "expires_in": 3600},
            )
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            os.environ,
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            result = await dispatch(
                tool=tool,
                llm_args={},
                http_client=client,
                auth_injector=injector,
                dispatch_config=_dispatch_cfg(),
            )

    assert call_count == 2
    assert result == [{"type": "text", "text": '{\n  "ok": true\n}'}]


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_401_without_oauth():
    """A 401 on a tool with no cached OAuth token is propagated unchanged."""
    from specmcp.config import BearerAuthConfig, SensitiveStr
    from specmcp.auth.injector import ResolvedScheme
    from specmcp.errors import UpstreamClientError

    op = _make_op(
        method="GET",
        path="/items",
        auth=[[AuthRequirement(scheme_name="myBearer")]],
    )
    tool = _make_tool(op, ArgumentMap(bindings={}))

    injector = AuthInjector(
        _schemes={
            "myBearer": ResolvedScheme(
                scheme_name="myBearer",
                config=BearerAuthConfig(type="bearer", value_from="env(X)"),
                credential=SensitiveStr("tok"),
            )
        },
        _token_caches={},
    )

    client = MagicMock()
    client.request = AsyncMock(
        side_effect=UpstreamClientError("Unauthorized", status_code=401)
    )

    with pytest.raises(UpstreamClientError) as exc_info:
        await dispatch(
            tool=tool,
            llm_args={},
            http_client=client,
            auth_injector=injector,
            dispatch_config=_dispatch_cfg(),
        )

    assert exc_info.value.status_code == 401
    assert client.request.call_count == 1  # no retry


@pytest.mark.asyncio
async def test_dispatch_raises_auth_error_if_retry_also_401():
    """If both attempts return 401, raises AuthError (not UpstreamClientError)."""
    import os
    import time
    from specmcp.auth.token_cache import CachedToken
    from specmcp.config import SensitiveStr
    from specmcp.errors import AuthError, UpstreamClientError

    tool, injector = _make_oauth_tool()
    injector._token_caches["myOAuth"]._token = CachedToken(
        access_token=SensitiveStr("stale-token"),
        expires_at=time.monotonic() + 3600,
    )

    client = MagicMock()
    client.request = AsyncMock(
        side_effect=UpstreamClientError("Unauthorized", status_code=401)
    )

    import respx, httpx

    with respx.mock:
        respx.post("https://auth.example.com/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "new-token", "expires_in": 3600},
            )
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            os.environ,
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            with pytest.raises(AuthError, match="after OAuth token refresh"):
                await dispatch(
                    tool=tool,
                    llm_args={},
                    http_client=client,
                    auth_injector=injector,
                    dispatch_config=_dispatch_cfg(),
                )

    assert client.request.call_count == 2  # original + one retry
