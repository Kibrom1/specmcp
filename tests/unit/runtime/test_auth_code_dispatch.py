"""Integration tests: AuthCodeHandler → dispatch() → mcp_error_content pipeline.

Verifies that when an oauth2_authorization_code scheme has no valid token:
  - AuthRequiredError is raised by the injector
  - The error carries the correct login_url (with nonce)
  - mcp_error_content() formats it into a user-readable message
  - The dispatcher (serve.py call_tool handler logic) converts it to a TextContent block
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specmcp.auth.injector import AuthInjector
from specmcp.auth.oauth2_authcode import AuthCodeHandler
from specmcp.auth.token_store import InMemoryTokenStore, OAuthTokens
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr
from specmcp.core.model import (
    ArgumentMap,
    AuthRequirement,
    Operation,
    Response,
    SimplifiedOperation,
)
from specmcp.core.expose import ToolDefinition, ToolRegistry
from specmcp.errors import AuthRequiredError, mcp_error_content


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

TOKEN_URL = "https://auth.example.com/token"
LOGIN_BASE = "http://localhost:8765"


def _make_cfg() -> OAuth2AuthorizationCodeConfig:
    return OAuth2AuthorizationCodeConfig(
        type="oauth2_authorization_code",
        authorization_url="https://auth.example.com/authorize",
        token_url=TOKEN_URL,
        client_id_from="env(C)",
        client_secret_from="env(S)",
        redirect_uri=f"{LOGIN_BASE}/auth/callback",
        scopes=["openid"],
    )


async def _issue_nonce(session_id: str, scheme_name: str) -> str:
    return f"nonce-{session_id}"


def _make_auth_code_injector(token_store: InMemoryTokenStore) -> AuthInjector:
    injector = AuthInjector.build(None)
    handler = AuthCodeHandler(
        scheme_name="userAuth",
        config=_make_cfg(),
        client_id=SensitiveStr("cid"),
        client_secret=SensitiveStr("csec"),
        token_store=token_store,
        issue_nonce=_issue_nonce,
        login_base_url=LOGIN_BASE,
    )
    injector.register_auth_code_handler("userAuth", handler)
    return injector


def _make_auth_tool() -> ToolDefinition:
    op = Operation(
        id="getSecureData",
        method="GET",
        path="/secure",
        server_url="https://api.example.com",
        parameters=[],
        responses=[Response(status_code="200", description="ok")],
        auth=[[AuthRequirement(scheme_name="userAuth", scopes=[])]],
    )
    sop = SimplifiedOperation(
        operation=op,
        llm_input_schema={"type": "object", "properties": {}, "required": []},
        llm_description="Get secure data",
        arg_map=ArgumentMap(bindings={}),
        warnings=[],
    )
    return ToolDefinition(
        name="getSecureData",
        description="Get secure data",
        input_schema={"type": "object", "properties": {}, "required": []},
        simplified_operation=sop,
    )


class _FakeSession:
    def __init__(self, session_id: str = "test-session"):
        self.session_id = session_id
        self.client_token = None


# ---------------------------------------------------------------------------
# AuthRequiredError message format
# ---------------------------------------------------------------------------


def test_auth_required_error_message_contains_login_url():
    """mcp_error_content for AuthRequiredError must embed the login_url."""
    exc = AuthRequiredError(
        session_id="sess-123",
        login_url=f"{LOGIN_BASE}/auth/login?nonce=abc123",
    )
    msg = mcp_error_content(exc)
    assert f"{LOGIN_BASE}/auth/login?nonce=abc123" in msg


def test_auth_required_error_message_instructs_user_to_log_in():
    """The formatted error must tell the user to visit the login URL."""
    exc = AuthRequiredError(
        session_id="sess",
        login_url="http://localhost:8765/auth/login?nonce=x",
    )
    msg = mcp_error_content(exc)
    assert "log in" in msg.lower() or "login" in msg.lower()


def test_auth_required_error_message_mentions_retry():
    """The formatted error must mention retrying after login."""
    exc = AuthRequiredError(
        session_id="sess",
        login_url="http://localhost:8765/auth/login?nonce=x",
    )
    msg = mcp_error_content(exc)
    assert "retry" in msg.lower() or "after" in msg.lower()


# ---------------------------------------------------------------------------
# Injector raises AuthRequiredError when no token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injector_raises_auth_required_when_no_token():
    """inject() for an auth code scheme with no token must raise AuthRequiredError."""
    store = InMemoryTokenStore()
    injector = _make_auth_code_injector(store)

    auth_req = [[AuthRequirement(scheme_name="userAuth", scopes=[])]]
    with pytest.raises(AuthRequiredError) as exc_info:
        await injector.inject(auth_req, headers={}, params={}, session=_FakeSession("nosess"))

    exc = exc_info.value
    assert exc.session_id == "nosess"
    assert exc.login_url is not None
    assert "nonce-nosess" in exc.login_url


@pytest.mark.asyncio
async def test_injector_injects_bearer_when_token_present():
    """inject() for auth code with a valid token must inject the Bearer header."""
    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("valid-tok"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("good-sess", tokens)

    injector = _make_auth_code_injector(store)
    auth_req = [[AuthRequirement(scheme_name="userAuth", scopes=[])]]
    headers, params = await injector.inject(
        auth_req, headers={}, params={}, session=_FakeSession("good-sess")
    )
    assert headers.get("Authorization") == "Bearer valid-tok"


# ---------------------------------------------------------------------------
# Full dispatch() pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_raises_auth_required_when_no_token():
    """dispatch() must propagate AuthRequiredError from auth injection."""
    from specmcp.config import DispatchConfig
    from specmcp.runtime.dispatcher import dispatch
    from specmcp.runtime.http_client import HttpClient

    store = InMemoryTokenStore()
    injector = _make_auth_code_injector(store)
    tool = _make_auth_tool()
    dispatch_cfg = DispatchConfig()

    with pytest.raises(AuthRequiredError) as exc_info:
        async with HttpClient(dispatch_cfg) as http_client:
            await dispatch(
                tool=tool,
                llm_args={},
                http_client=http_client,
                auth_injector=injector,
                dispatch_config=dispatch_cfg,
                session=_FakeSession("dispatch-sess"),
            )

    assert exc_info.value.session_id == "dispatch-sess"
    assert exc_info.value.login_url is not None


@pytest.mark.asyncio
async def test_dispatch_auth_required_formats_to_text_block():
    """AuthRequiredError caught at the serve level must produce a text block with login URL."""
    import mcp.types as mcp_types
    from specmcp.config import DispatchConfig
    from specmcp.runtime.dispatcher import dispatch
    from specmcp.runtime.http_client import HttpClient

    store = InMemoryTokenStore()
    injector = _make_auth_code_injector(store)
    tool = _make_auth_tool()
    dispatch_cfg = DispatchConfig()

    # Simulate the serve.py call_tool exception handler
    error_text: str | None = None
    try:
        async with HttpClient(dispatch_cfg) as http_client:
            await dispatch(
                tool=tool,
                llm_args={},
                http_client=http_client,
                auth_injector=injector,
                dispatch_config=dispatch_cfg,
                session=_FakeSession("fmt-sess"),
            )
    except AuthRequiredError as exc:
        error_text = mcp_error_content(exc)

    assert error_text is not None
    # The LLM must receive the login URL
    assert "nonce-fmt-sess" in error_text
    assert LOGIN_BASE in error_text


@pytest.mark.asyncio
async def test_dispatch_succeeds_after_token_stored():
    """dispatch() must succeed (reach HTTP) when a valid token exists for the session."""
    import respx
    import httpx
    from specmcp.config import DispatchConfig
    from specmcp.runtime.dispatcher import dispatch
    from specmcp.runtime.http_client import HttpClient

    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("live-token"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("live-sess", tokens)

    injector = _make_auth_code_injector(store)
    tool = _make_auth_tool()
    dispatch_cfg = DispatchConfig()

    with respx.mock:
        respx.get("https://api.example.com/secure").mock(
            return_value=httpx.Response(200, json={"data": "secret"})
        )
        async with HttpClient(dispatch_cfg) as http_client:
            result = await dispatch(
                tool=tool,
                llm_args={},
                http_client=http_client,
                auth_injector=injector,
                dispatch_config=dispatch_cfg,
                session=_FakeSession("live-sess"),
            )

    assert len(result) == 1
    assert "secret" in result[0]["text"]


@pytest.mark.asyncio
async def test_dispatch_injects_correct_bearer_token():
    """dispatch() must send the stored access_token as Authorization: Bearer."""
    import respx
    import httpx
    from specmcp.config import DispatchConfig
    from specmcp.runtime.dispatcher import dispatch
    from specmcp.runtime.http_client import HttpClient

    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("my-bearer-token"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("bearer-sess", tokens)

    injector = _make_auth_code_injector(store)
    tool = _make_auth_tool()
    dispatch_cfg = DispatchConfig()

    captured_request: list[httpx.Request] = []

    with respx.mock:
        route = respx.get("https://api.example.com/secure").mock(
            return_value=httpx.Response(200, json={})
        )
        async with HttpClient(dispatch_cfg) as http_client:
            await dispatch(
                tool=tool,
                llm_args={},
                http_client=http_client,
                auth_injector=injector,
                dispatch_config=dispatch_cfg,
                session=_FakeSession("bearer-sess"),
            )
        req = route.calls[0].request

    assert req.headers.get("authorization") == "Bearer my-bearer-token"


# ---------------------------------------------------------------------------
# No session → AuthConfigError (not AuthRequiredError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_no_session_raises_auth_config_error():
    """dispatch() with session=None for an auth code scheme raises AuthConfigError."""
    from specmcp.config import DispatchConfig
    from specmcp.errors import AuthConfigError
    from specmcp.runtime.dispatcher import dispatch
    from specmcp.runtime.http_client import HttpClient

    store = InMemoryTokenStore()
    injector = _make_auth_code_injector(store)
    tool = _make_auth_tool()
    dispatch_cfg = DispatchConfig()

    with pytest.raises(AuthConfigError, match="requires an active session"):
        async with HttpClient(dispatch_cfg) as http_client:
            await dispatch(
                tool=tool,
                llm_args={},
                http_client=http_client,
                auth_injector=injector,
                dispatch_config=dispatch_cfg,
                session=None,  # No session
            )
