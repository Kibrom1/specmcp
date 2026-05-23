"""
Phase 2 tests: bearer token priority via SessionContext.client_token.

These tests verify that when a session carries a client_token (from MCP
initialize._meta.bearer_token), the injector uses it instead of the
env-var configured token.

The _BearerHandler in injector.py was already written to check
session.client_token in Phase 1. These tests prove that contract.
"""

from __future__ import annotations

import pytest

from specmcp.auth.injector import AuthInjector, ResolvedScheme
from specmcp.config import BearerAuthConfig, SensitiveStr
from specmcp.core.model import AuthRequirement
from specmcp.runtime.session import SessionContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer_injector(env_token: str = "env-var-token") -> AuthInjector:
    """Build an AuthInjector with one Bearer scheme backed by an env-var token."""
    cfg = BearerAuthConfig(type="bearer", value_from="env(MY_BEARER)")
    resolved = ResolvedScheme(
        scheme_name="myBearer",
        config=cfg,
        credential=SensitiveStr(env_token),
    )
    return AuthInjector(_schemes={"myBearer": resolved}, _token_caches={})


def _auth_reqs(scheme: str = "myBearer") -> list[list[AuthRequirement]]:
    return [[AuthRequirement(scheme_name=scheme)]]


# ---------------------------------------------------------------------------
# BearerHandler — session.client_token takes priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_uses_env_token_when_no_session():
    """Without a session, the env-var token is used."""
    injector = _bearer_injector(env_token="env-token-xyz")
    h, _ = await injector.inject(_auth_reqs(), headers={}, params={}, session=None)
    assert h["Authorization"] == "Bearer env-token-xyz"


@pytest.mark.asyncio
async def test_bearer_uses_env_token_when_session_has_no_client_token():
    """Session without client_token falls back to the env-var token."""
    injector = _bearer_injector(env_token="env-token-xyz")
    session = SessionContext(session_id="s1")  # no client_token
    h, _ = await injector.inject(_auth_reqs(), headers={}, params={}, session=session)
    assert h["Authorization"] == "Bearer env-token-xyz"


@pytest.mark.asyncio
async def test_bearer_uses_client_token_over_env_token():
    """client_token takes priority over the env-var token when present."""
    injector = _bearer_injector(env_token="env-token-xyz")
    session = SessionContext(
        session_id="s1",
        client_token=SensitiveStr("client-supplied-token"),
    )
    h, _ = await injector.inject(_auth_reqs(), headers={}, params={}, session=session)
    assert h["Authorization"] == "Bearer client-supplied-token"


@pytest.mark.asyncio
async def test_bearer_client_token_not_logged_in_header_value():
    """The actual token value must not appear in the SensitiveStr repr."""
    session = SessionContext(
        session_id="s1",
        client_token=SensitiveStr("super-secret-client-token"),
    )
    # confirm SensitiveStr redaction still works
    assert "super-secret-client-token" not in str(session.client_token)
    assert "super-secret-client-token" not in repr(session.client_token)
    # but reveal() works
    assert session.client_token.reveal() == "super-secret-client-token"


@pytest.mark.asyncio
async def test_bearer_different_sessions_get_different_tokens():
    """Two sessions with different client_tokens receive their own token."""
    injector = _bearer_injector(env_token="env-token")
    s1 = SessionContext(session_id="s1", client_token=SensitiveStr("token-for-s1"))
    s2 = SessionContext(session_id="s2", client_token=SensitiveStr("token-for-s2"))

    h1, _ = await injector.inject(_auth_reqs(), headers={}, params={}, session=s1)
    h2, _ = await injector.inject(_auth_reqs(), headers={}, params={}, session=s2)

    assert h1["Authorization"] == "Bearer token-for-s1"
    assert h2["Authorization"] == "Bearer token-for-s2"


# ---------------------------------------------------------------------------
# MCP initialize._meta bearer_token extraction (unit-level simulation)
# ---------------------------------------------------------------------------


def test_client_params_meta_bearer_token_accessible():
    """Verify the MCP SDK exposes bearer_token via params.meta.model_extra."""
    import mcp.types as t

    params = t.InitializeRequestParams.model_validate({
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
        "_meta": {"bearer_token": "init-meta-token"},
    })
    assert params.meta is not None
    bearer = params.meta.model_extra.get("bearer_token")
    assert bearer == "init-meta-token"


def test_client_params_meta_absent_when_not_passed():
    """When no _meta is passed, params.meta is None."""
    import mcp.types as t

    params = t.InitializeRequestParams.model_validate({
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    })
    assert params.meta is None


def test_client_params_meta_without_bearer_token():
    """_meta without bearer_token returns None from model_extra.get()."""
    import mcp.types as t

    params = t.InitializeRequestParams.model_validate({
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
        "_meta": {"progressToken": "pt-123"},
    })
    assert params.meta is not None
    assert params.meta.model_extra.get("bearer_token") is None
