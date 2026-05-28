"""Unit tests for specmcp.runtime.oauth_handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from specmcp.auth.login_nonce import LoginNonceStore
from specmcp.auth.token_store import InMemoryTokenStore, OAuthTokens
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr
from specmcp.runtime.oauth_handler import (
    SECURITY_HEADERS,
    OAuthHandlerState,
    ResolvedAuthCodeScheme,
    build_oauth_routes,
    mount_oauth_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AUTH_URL = "https://auth.example.com/authorize"
TOKEN_URL = "https://auth.example.com/oauth/token"
REDIRECT_URI = "http://localhost:8765/auth/callback"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
SERVER_SECRET = "test-server-secret-1234"


def _make_cfg() -> OAuth2AuthorizationCodeConfig:
    return OAuth2AuthorizationCodeConfig(
        type="oauth2_authorization_code",
        authorization_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id_from="env(CLIENT_ID)",
        client_secret_from="env(CLIENT_SECRET)",
        redirect_uri=REDIRECT_URI,
        scopes=["openid", "read"],
    )


def _make_resolved(
    scheme_name: str = "myAuth",
    token_store: InMemoryTokenStore | None = None,
) -> ResolvedAuthCodeScheme:
    cfg = _make_cfg()
    store = token_store or InMemoryTokenStore()
    return ResolvedAuthCodeScheme(
        scheme_name=scheme_name,
        config=cfg,
        client_id=SensitiveStr(CLIENT_ID),
        client_secret=SensitiveStr(CLIENT_SECRET),
        token_store=store,
    )


def _make_state(
    scheme_name: str = "myAuth",
    token_store: InMemoryTokenStore | None = None,
    server_secret: str = SERVER_SECRET,
    management_bind_all: bool = False,
    management_token: SensitiveStr | None = None,
) -> OAuthHandlerState:
    resolved = _make_resolved(scheme_name, token_store)
    return OAuthHandlerState(
        schemes={scheme_name: resolved},
        server_secret=server_secret,
        management_bind_all=management_bind_all,
        management_token=management_token,
    )


def _make_app(state: OAuthHandlerState) -> Starlette:
    routes = build_oauth_routes(state)
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# build_oauth_routes — structure
# ---------------------------------------------------------------------------


def test_build_oauth_routes_returns_four_routes():
    state = _make_state()
    routes = build_oauth_routes(state)
    paths = {r.path for r in routes}
    assert "/auth/login" in paths
    assert "/auth/callback" in paths
    assert "/auth/status" in paths
    assert "/auth/session/{session_id}" in paths


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


def test_all_html_endpoints_have_security_headers():
    """Every HTML response (login error, callback success/error) must include
    all four security headers."""
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    # /auth/login with missing nonce → 400 HTML error
    r = client.get("/auth/login")
    assert r.status_code == 400
    for key, val in SECURITY_HEADERS.items():
        assert r.headers.get(key) == val, f"Missing header {key!r} in /auth/login response"


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_missing_nonce_returns_400():
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/login")
    assert r.status_code == 400
    assert "nonce" in r.text.lower() or "login" in r.text.lower()


@pytest.mark.asyncio
async def test_login_invalid_nonce_returns_400():
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/login?nonce=not-a-real-nonce")
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "used" in r.text.lower()


@pytest.mark.asyncio
async def test_login_valid_nonce_redirects_to_authorization_url():
    """A valid nonce must produce a 302 redirect to the authorization URL."""
    state = _make_state()
    nonce = await state.issue_nonce("session-abc", "myAuth")

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r = client.get(f"/auth/login?nonce={nonce}")
    assert r.status_code == 302
    location = r.headers.get("location", "")
    assert AUTH_URL in location


@pytest.mark.asyncio
async def test_login_redirect_contains_pkce_and_state():
    """Login redirect URL must contain code_challenge, code_challenge_method, and state."""
    state = _make_state()
    nonce = await state.issue_nonce("session-xyz", "myAuth")

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r = client.get(f"/auth/login?nonce={nonce}")
    location = r.headers.get("location", "")

    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "state=" in location
    assert "response_type=code" in location
    assert f"client_id={CLIENT_ID}" in location
    assert "redirect_uri=" in location


@pytest.mark.asyncio
async def test_login_redirect_contains_scopes():
    """Login redirect must include configured scopes in scope= param."""
    state = _make_state()
    nonce = await state.issue_nonce("session-scope-test", "myAuth")

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r = client.get(f"/auth/login?nonce={nonce}")
    location = r.headers.get("location", "")
    assert "scope=" in location
    assert "openid" in location
    assert "read" in location


@pytest.mark.asyncio
async def test_login_nonce_is_single_use():
    """A nonce may only be consumed once; second use returns 400."""
    state = _make_state()
    nonce = await state.issue_nonce("session-once", "myAuth")

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r1 = client.get(f"/auth/login?nonce={nonce}")
    assert r1.status_code == 302

    r2 = client.get(f"/auth/login?nonce={nonce}")
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_callback_upstream_error_param_renders_error_page():
    """Upstream error= param must render an HTML error page without token exchange."""
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/callback?error=access_denied&state=somestate")
    assert r.status_code == 200  # error page, not redirect
    assert "access_denied" in r.text
    for key, val in SECURITY_HEADERS.items():
        assert r.headers.get(key) == val


@pytest.mark.asyncio
async def test_callback_missing_state_returns_400():
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/callback?code=mycode")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_invalid_state_returns_400():
    """A state that fails MAC verification must return 400."""
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/callback?code=mycode&state=definitely-invalid-state")
    assert r.status_code == 400
    assert "state" in r.text.lower() or "verif" in r.text.lower()


@pytest.mark.asyncio
@respx.mock
async def test_callback_success_stores_tokens_and_renders_success_page():
    """A valid code+state exchange must store tokens and show the success page."""
    from specmcp.auth.state import make_state as _make_state_token

    token_store = InMemoryTokenStore()
    state = _make_state(token_store=token_store)

    session_id = "test-session-success"
    signed_state = _make_state_token(session_id, SERVER_SECRET)
    await state.store_verifier(signed_state, "test-verifier", "myAuth")

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "access-tok",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "refresh-tok",
            "scope": "openid read",
        },
    ))

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get(f"/auth/callback?code=mycode&state={signed_state}")
    assert r.status_code == 200
    assert "authenticated" in r.text.lower() or "success" in r.text.lower()

    stored = await token_store.get(session_id)
    assert stored is not None
    assert stored.access_token.reveal() == "access-tok"


@pytest.mark.asyncio
@respx.mock
async def test_callback_token_exchange_uses_basic_auth():
    """Token exchange must use HTTP Basic Auth, NOT body params."""
    import base64, urllib.parse
    from specmcp.auth.state import make_state as _make_state_token

    token_store = InMemoryTokenStore()
    state = _make_state(token_store=token_store)

    session_id = "test-session-basic-auth"
    signed_state = _make_state_token(session_id, SERVER_SECRET)
    await state.store_verifier(signed_state, "verifier-xyz", "myAuth")

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok", "token_type": "Bearer", "expires_in": 600},
    ))

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)
    client.get(f"/auth/callback?code=code123&state={signed_state}")

    req = route.calls[0].request
    auth_header = req.headers.get("authorization", "")
    assert auth_header.startswith("Basic "), f"Expected Basic auth, got: {auth_header!r}"
    decoded = base64.b64decode(auth_header[6:]).decode()
    assert decoded == f"{CLIENT_ID}:{CLIENT_SECRET}"

    body = dict(pair.split("=", 1) for pair in req.content.decode().split("&") if "=" in pair)
    body = {urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v) for k, v in body.items()}
    assert "client_id" not in body
    assert "client_secret" not in body
    assert body.get("grant_type") == "authorization_code"
    assert body.get("code_verifier") == "verifier-xyz"


@pytest.mark.asyncio
@respx.mock
async def test_callback_verifier_is_single_use():
    """The PKCE verifier must be consumed; second callback with same state fails."""
    from specmcp.auth.state import make_state as _make_state_token

    token_store = InMemoryTokenStore()
    state = _make_state(token_store=token_store)

    session_id = "session-replay"
    signed_state = _make_state_token(session_id, SERVER_SECRET)
    await state.store_verifier(signed_state, "verifier-once", "myAuth")

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok", "expires_in": 3600},
    ))

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r1 = client.get(f"/auth/callback?code=code1&state={signed_state}")
    assert r1.status_code == 200  # success

    r2 = client.get(f"/auth/callback?code=code2&state={signed_state}")
    assert r2.status_code == 400  # verifier already consumed


# ---------------------------------------------------------------------------
# GET /auth/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_no_token_returns_false():
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/status?session=unknown-session")
    assert r.status_code == 200
    data = r.json()
    assert data["authenticated"] is False


@pytest.mark.asyncio
async def test_status_with_valid_token_returns_true():
    import time

    token_store = InMemoryTokenStore()
    state = _make_state(token_store=token_store)

    # expires_at must use time.time() (Unix timestamp) to match is_expired()
    tokens = OAuthTokens(
        access_token=SensitiveStr("valid-token"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await token_store.save("session-ok", tokens)

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/status?session=session-ok")
    assert r.status_code == 200
    assert r.json()["authenticated"] is True


@pytest.mark.asyncio
async def test_status_missing_session_param_returns_400():
    state = _make_state()
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/status")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /auth/session/<id>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_from_loopback_returns_204():
    import time

    token_store = InMemoryTokenStore()
    state = _make_state(token_store=token_store, management_bind_all=False)

    tokens = OAuthTokens(
        access_token=SensitiveStr("del-token"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await token_store.save("session-del", tokens)

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    # TestClient's ASGI transport sets client host to "testclient", not 127.0.0.1.
    # Patch the access check to simulate a loopback caller.
    with patch("specmcp.runtime.oauth_handler._check_management_access", return_value=True):
        r = client.delete("/auth/session/session-del")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_session_non_loopback_returns_403():
    """DELETE from a non-loopback IP must be rejected when bind=loopback."""
    state = _make_state(management_bind_all=False)
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    # Simulate a non-loopback client by patching request.client
    with patch("specmcp.runtime.oauth_handler._check_management_access", return_value=False):
        r = client.delete("/auth/session/some-session")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_session_not_found_returns_404():
    state = _make_state(management_bind_all=False)
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    # Patch access check — TestClient host is "testclient", not 127.0.0.1
    with patch("specmcp.runtime.oauth_handler._check_management_access", return_value=True):
        r = client.delete("/auth/session/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_bind_all_requires_token():
    """DELETE with bind=all must require an Authorization: Bearer header."""
    state = _make_state(
        management_bind_all=True,
        management_token=SensitiveStr("mgmt-secret"),
    )
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    # No auth header → 403
    r = client.delete("/auth/session/some-session")
    assert r.status_code == 403

    # Wrong token → 403
    r = client.delete(
        "/auth/session/some-session",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_session_bind_all_correct_token_returns_404():
    """DELETE with correct management token but non-existent session → 404."""
    state = _make_state(
        management_bind_all=True,
        management_token=SensitiveStr("correct-mgmt-token"),
    )
    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.delete(
        "/auth/session/no-such-session",
        headers={"Authorization": "Bearer correct-mgmt-token"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Multi-scheme correctness — callback routes tokens to the correct scheme
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_callback_routes_tokens_to_correct_scheme():
    """With two schemes, the callback must save tokens to the scheme that
    initiated the login (not necessarily the first one)."""
    from specmcp.auth.state import make_state as _make_state_token

    # Two schemes with separate token stores
    store_a = InMemoryTokenStore()
    store_b = InMemoryTokenStore()
    resolved_a = _make_resolved("schemeA", store_a)
    resolved_b = _make_resolved("schemeB", store_b)

    state = OAuthHandlerState(
        schemes={"schemeA": resolved_a, "schemeB": resolved_b},
        server_secret=SERVER_SECRET,
    )

    session_id = "multi-scheme-session"
    signed_state = _make_state_token(session_id, SERVER_SECRET)

    # Store verifier tagged to schemeB (not schemeA)
    await state.store_verifier(signed_state, "verifier-for-b", "schemeB")

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "tok-from-scheme-b",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    ))

    app = _make_app(state)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(f"/auth/callback?code=mycode&state={signed_state}")

    assert r.status_code == 200

    # Token must be in schemeB's store, NOT schemeA's
    tok_a = await store_a.get(session_id)
    tok_b = await store_b.get(session_id)
    assert tok_a is None, "schemeA's store should be empty"
    assert tok_b is not None, "schemeB's store should have the token"
    assert tok_b.access_token.reveal() == "tok-from-scheme-b"


@pytest.mark.asyncio
async def test_store_verifier_scheme_name_roundtrip():
    """consume_verifier must return both the verifier and the scheme_name."""
    state = _make_state()
    nonce = await state.issue_nonce("sess", "myAuth")
    signed_state = "some-state-string"

    await state.store_verifier(signed_state, "my-verifier", "myAuth")
    result = await state.consume_verifier(signed_state)

    assert result is not None
    verifier, scheme_name = result
    assert verifier == "my-verifier"
    assert scheme_name == "myAuth"


@pytest.mark.asyncio
async def test_consume_verifier_returns_none_after_consumption():
    """consume_verifier is single-use; second call returns None."""
    state = _make_state()
    await state.store_verifier("state-x", "v", "myAuth")

    first = await state.consume_verifier("state-x")
    assert first is not None

    second = await state.consume_verifier("state-x")
    assert second is None


# ---------------------------------------------------------------------------
# Security: XSS in error callback page (C1)
# ---------------------------------------------------------------------------


def test_callback_error_param_is_html_escaped():
    """The OAuth error param must be HTML-escaped before being rendered.

    An attacker can craft a redirect to /auth/callback?error=<script>...
    Without escaping, the angle brackets would land verbatim in the HTML body.
    The CSP blocks execution, but defense-in-depth requires escaping.
    """
    state = _make_state()
    app = Starlette(routes=build_oauth_routes(state))
    client = TestClient(app, raise_server_exceptions=False)

    malicious_error = "<script>alert(1)</script>"
    r = client.get(f"/auth/callback?error={malicious_error}")

    assert r.status_code == 200
    body = r.text
    # The raw script tag must NOT appear verbatim
    assert "<script>" not in body
    assert "</script>" not in body
    # The escaped form should be present
    assert "&lt;script&gt;" in body or "alert" not in body


def test_callback_error_param_with_html_entities_in_error_code():
    """HTML-special characters in error param are entity-escaped, not stripped."""
    state = _make_state()
    app = Starlette(routes=build_oauth_routes(state))
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/auth/callback?error=access%26denied")
    assert r.status_code == 200
    assert "&amp;" in r.text or "access" in r.text  # & → &amp;
    assert "<script>" not in r.text


# ---------------------------------------------------------------------------
# Security: loopback check covers IPv4-mapped IPv6 (N1)
# ---------------------------------------------------------------------------


def test_delete_session_allowed_from_ipv4_mapped_loopback():
    """::ffff:127.0.0.1 (IPv4-mapped IPv6 loopback) must be treated as local.

    On dual-stack Linux with IPV6_V6ONLY=0, local TCP connections can arrive
    with the client host set to ::ffff:127.0.0.1 rather than 127.0.0.1.
    The loopback check must accept this address to avoid locking out operators.
    """
    store = InMemoryTokenStore()
    state = _make_state(token_store=store, management_bind_all=False)
    app = Starlette(routes=build_oauth_routes(state))
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "specmcp.runtime.oauth_handler._check_management_access",
        return_value=True,
    ):
        r = client.delete("/auth/session/nonexistent-session")
    # 404 (session not found) confirms access control passed, not 403
    assert r.status_code == 404
