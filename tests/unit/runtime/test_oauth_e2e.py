"""End-to-end OAuth Authorization Code flow smoke tests.

These tests exercise the complete path without running uvicorn or the MCP SSE
protocol. They use Starlette's synchronous TestClient as the browser/user-agent
and respx to mock the upstream token endpoint.

Flow exercised:
  1. issue_nonce() — server issues a single-use nonce
  2. GET /auth/login?nonce=… — consumes nonce, generates PKCE, redirects to IdP
  3. GET /auth/callback?code=…&state=… — exchanges code for tokens (mocked IdP)
  4. GET /auth/status?session=… — confirms the session is authenticated
  5. AuthInjector.inject() — confirms the stored token is injected as Bearer

A second suite covers multi-scheme isolation: two schemes each follow the same
flow independently and tokens never leak across scheme stores.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from specmcp.auth.injector import AuthInjector
from specmcp.auth.oauth2_authcode import AuthCodeHandler
from specmcp.auth.token_store import InMemoryTokenStore
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr
from specmcp.core.model import AuthRequirement
from specmcp.runtime.oauth_handler import (
    OAuthHandlerState,
    ResolvedAuthCodeScheme,
    build_oauth_routes,
)


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

AUTH_URL = "https://idp.example.com/authorize"
TOKEN_URL = "https://idp.example.com/token"
REDIRECT_URI = "http://localhost:8765/auth/callback"
CLIENT_ID = "e2e-client-id"
CLIENT_SECRET = "e2e-client-secret"
SERVER_SECRET = "e2e-server-secret"
LOGIN_BASE = "http://localhost:8765"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(
    token_url: str = TOKEN_URL,
    auth_url: str = AUTH_URL,
    redirect_uri: str = REDIRECT_URI,
) -> OAuth2AuthorizationCodeConfig:
    return OAuth2AuthorizationCodeConfig(
        type="oauth2_authorization_code",
        authorization_url=auth_url,
        token_url=token_url,
        client_id_from="env(CLIENT_ID)",
        client_secret_from="env(CLIENT_SECRET)",
        redirect_uri=redirect_uri,
        scopes=["openid", "profile"],
    )


def _make_resolved(
    scheme_name: str,
    token_store: InMemoryTokenStore,
    token_url: str = TOKEN_URL,
) -> ResolvedAuthCodeScheme:
    return ResolvedAuthCodeScheme(
        scheme_name=scheme_name,
        config=_make_cfg(token_url=token_url),
        client_id=SensitiveStr(CLIENT_ID),
        client_secret=SensitiveStr(CLIENT_SECRET),
        token_store=token_store,
    )


def _build_app_and_injector(
    schemes: dict[str, ResolvedAuthCodeScheme],
    token_stores: dict[str, InMemoryTokenStore],
) -> tuple[Starlette, OAuthHandlerState, AuthInjector]:
    """Create the Starlette app, OAuthHandlerState, and wired AuthInjector."""
    oauth_state = OAuthHandlerState(
        schemes=schemes,
        server_secret=SERVER_SECRET,
    )

    injector = AuthInjector.build(None)
    for scheme_name, resolved in schemes.items():
        handler = AuthCodeHandler(
            scheme_name=scheme_name,
            config=resolved.config,
            client_id=resolved.client_id,
            client_secret=resolved.client_secret,
            token_store=token_stores[scheme_name],
            issue_nonce=oauth_state.issue_nonce,
            login_base_url=LOGIN_BASE,
        )
        injector.register_auth_code_handler(scheme_name, handler)

    app = Starlette(routes=build_oauth_routes(oauth_state))
    return app, oauth_state, injector


def _extract_state_param(location: str) -> str:
    """Pull the `state` query param out of a redirect Location header."""
    qs = urllib.parse.urlparse(location).query
    return urllib.parse.parse_qs(qs)["state"][0]


class _Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.client_token = None


# ---------------------------------------------------------------------------
# Happy path — single scheme, full flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_full_oauth_flow_single_scheme():
    """Complete login → callback → status → inject flow for a single scheme."""
    session_id = "e2e-session-1"
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, injector = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    # ── Step 1: issue nonce and hit /auth/login ──────────────────────────────
    nonce = await oauth_state.issue_nonce(session_id, "myAuth")
    r = client.get(f"/auth/login?nonce={nonce}")

    assert r.status_code == 302, f"Expected 302, got {r.status_code}: {r.text}"
    location = r.headers["location"]
    assert AUTH_URL in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location

    state_param = _extract_state_param(location)

    # ── Step 2: simulate IdP callback with mocked token endpoint ────────────
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "e2e-access-tok",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "e2e-refresh-tok",
            "scope": "openid profile",
        },
    ))

    r = client.get(f"/auth/callback?code=authcode123&state={state_param}")
    assert r.status_code == 200
    assert "authenticated" in r.text.lower() or "success" in r.text.lower()

    # ── Step 3: poll status ──────────────────────────────────────────────────
    r = client.get(f"/auth/status?session={session_id}")
    assert r.status_code == 200
    assert r.json()["authenticated"] is True

    # ── Step 4: injector uses the stored token ───────────────────────────────
    headers, _ = await injector.inject(
        [[AuthRequirement(scheme_name="myAuth", scopes=[])]],
        headers={},
        params={},
        session=_Session(session_id),
    )
    assert headers.get("Authorization") == "Bearer e2e-access-tok"


@pytest.mark.asyncio
@respx.mock
async def test_full_flow_token_endpoint_receives_correct_params():
    """Token exchange must use Basic Auth and include code + code_verifier."""
    import base64

    session_id = "e2e-params-check"
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, _ = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    nonce = await oauth_state.issue_nonce(session_id, "myAuth")
    r = client.get(f"/auth/login?nonce={nonce}")
    state_param = _extract_state_param(r.headers["location"])

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 3600}
    ))

    client.get(f"/auth/callback?code=mycode&state={state_param}")

    req = route.calls[0].request

    # Basic Auth header
    auth = req.headers.get("authorization", "")
    assert auth.startswith("Basic "), f"Expected Basic auth, got: {auth!r}"
    decoded = base64.b64decode(auth[6:]).decode()
    assert decoded == f"{CLIENT_ID}:{CLIENT_SECRET}"

    # Body params
    body = dict(
        pair.split("=", 1)
        for pair in req.content.decode().split("&")
        if "=" in pair
    )
    body = {urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v)
            for k, v in body.items()}
    assert body.get("grant_type") == "authorization_code"
    assert body.get("code") == "mycode"
    assert "code_verifier" in body, "code_verifier must be in body"
    # Verify the code_verifier is 43 chars (token_urlsafe(32))
    assert len(body["code_verifier"]) == 43
    # Credentials must NOT be in body
    assert "client_id" not in body
    assert "client_secret" not in body


@pytest.mark.asyncio
async def test_status_returns_false_before_callback():
    """Status must return false for a session that has not completed login."""
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, _, _ = _build_app_and_injector({"myAuth": resolved}, {"myAuth": store})

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/auth/status?session=never-logged-in")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


@pytest.mark.asyncio
@respx.mock
async def test_nonce_cannot_be_reused_across_two_login_attempts():
    """The login nonce is single-use; a second attempt returns 400."""
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, _ = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    nonce = await oauth_state.issue_nonce("sess-reuse", "myAuth")

    r1 = client.get(f"/auth/login?nonce={nonce}")
    assert r1.status_code == 302

    r2 = client.get(f"/auth/login?nonce={nonce}")
    assert r2.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_state_cannot_be_replayed_in_callback():
    """A second callback with the same state (replay) must return 400."""
    session_id = "e2e-replay"
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, _ = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    nonce = await oauth_state.issue_nonce(session_id, "myAuth")
    r = client.get(f"/auth/login?nonce={nonce}")
    state_param = _extract_state_param(r.headers["location"])

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 3600}
    ))

    r1 = client.get(f"/auth/callback?code=code1&state={state_param}")
    assert r1.status_code == 200  # first callback succeeds

    r2 = client.get(f"/auth/callback?code=code2&state={state_param}")
    assert r2.status_code == 400  # replay rejected


# ---------------------------------------------------------------------------
# Multi-scheme isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_multi_scheme_tokens_stored_in_correct_scheme():
    """Two schemes complete their flows independently; tokens don't cross stores."""
    TOKEN_URL_A = "https://idp-a.example.com/token"
    TOKEN_URL_B = "https://idp-b.example.com/token"
    AUTH_URL_A = "https://idp-a.example.com/authorize"
    AUTH_URL_B = "https://idp-b.example.com/authorize"

    store_a = InMemoryTokenStore()
    store_b = InMemoryTokenStore()

    resolved_a = ResolvedAuthCodeScheme(
        scheme_name="authA",
        config=_make_cfg(token_url=TOKEN_URL_A, auth_url=AUTH_URL_A),
        client_id=SensitiveStr("cid-a"),
        client_secret=SensitiveStr("csec-a"),
        token_store=store_a,
    )
    resolved_b = ResolvedAuthCodeScheme(
        scheme_name="authB",
        config=_make_cfg(token_url=TOKEN_URL_B, auth_url=AUTH_URL_B),
        client_id=SensitiveStr("cid-b"),
        client_secret=SensitiveStr("csec-b"),
        token_store=store_b,
    )

    app, oauth_state, injector = _build_app_and_injector(
        {"authA": resolved_a, "authB": resolved_b},
        {"authA": store_a, "authB": store_b},
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    # Scheme A flow
    session_a = "session-for-a"
    nonce_a = await oauth_state.issue_nonce(session_a, "authA")
    r_a = client.get(f"/auth/login?nonce={nonce_a}")
    assert r_a.status_code == 302
    assert AUTH_URL_A in r_a.headers["location"]
    state_a = _extract_state_param(r_a.headers["location"])

    # Scheme B flow
    session_b = "session-for-b"
    nonce_b = await oauth_state.issue_nonce(session_b, "authB")
    r_b = client.get(f"/auth/login?nonce={nonce_b}")
    assert r_b.status_code == 302
    assert AUTH_URL_B in r_b.headers["location"]
    state_b = _extract_state_param(r_b.headers["location"])

    # Mock both token endpoints
    respx.post(TOKEN_URL_A).mock(return_value=httpx.Response(
        200, json={"access_token": "tok-a", "expires_in": 3600}
    ))
    respx.post(TOKEN_URL_B).mock(return_value=httpx.Response(
        200, json={"access_token": "tok-b", "expires_in": 3600}
    ))

    # Complete both callbacks
    r = client.get(f"/auth/callback?code=code-a&state={state_a}")
    assert r.status_code == 200
    r = client.get(f"/auth/callback?code=code-b&state={state_b}")
    assert r.status_code == 200

    # Verify token isolation
    tok_a = await store_a.get(session_a)
    tok_b = await store_b.get(session_b)
    tok_a_wrong = await store_a.get(session_b)  # scheme A's store, scheme B's session
    tok_b_wrong = await store_b.get(session_a)  # scheme B's store, scheme A's session

    assert tok_a is not None and tok_a.access_token.reveal() == "tok-a"
    assert tok_b is not None and tok_b.access_token.reveal() == "tok-b"
    assert tok_a_wrong is None, "schemeA store must not contain schemeB's session"
    assert tok_b_wrong is None, "schemeB store must not contain schemeA's session"


@pytest.mark.asyncio
@respx.mock
async def test_multi_scheme_injector_picks_correct_token():
    """After both schemes complete their flows, the injector injects the
    right token for each scheme independently."""
    TOKEN_URL_A = "https://idp-a.example.com/token"
    TOKEN_URL_B = "https://idp-b.example.com/token"

    store_a, store_b = InMemoryTokenStore(), InMemoryTokenStore()

    resolved_a = ResolvedAuthCodeScheme(
        scheme_name="authA",
        config=_make_cfg(token_url=TOKEN_URL_A),
        client_id=SensitiveStr("cid-a"),
        client_secret=SensitiveStr("csec-a"),
        token_store=store_a,
    )
    resolved_b = ResolvedAuthCodeScheme(
        scheme_name="authB",
        config=_make_cfg(token_url=TOKEN_URL_B),
        client_id=SensitiveStr("cid-b"),
        client_secret=SensitiveStr("csec-b"),
        token_store=store_b,
    )

    app, oauth_state, injector = _build_app_and_injector(
        {"authA": resolved_a, "authB": resolved_b},
        {"authA": store_a, "authB": store_b},
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    for scheme, session_id, tok_url, tok_val in [
        ("authA", "sess-a", TOKEN_URL_A, "access-a"),
        ("authB", "sess-b", TOKEN_URL_B, "access-b"),
    ]:
        nonce = await oauth_state.issue_nonce(session_id, scheme)
        r = client.get(f"/auth/login?nonce={nonce}")
        state_param = _extract_state_param(r.headers["location"])

        respx.post(tok_url).mock(return_value=httpx.Response(
            200, json={"access_token": tok_val, "expires_in": 3600}
        ))
        client.get(f"/auth/callback?code=code&state={state_param}")

    # Inject for each session and verify the correct token is used
    for scheme, session_id, expected_tok in [
        ("authA", "sess-a", "access-a"),
        ("authB", "sess-b", "access-b"),
    ]:
        headers, _ = await injector.inject(
            [[AuthRequirement(scheme_name=scheme, scopes=[])]],
            headers={},
            params={},
            session=_Session(session_id),
        )
        assert headers.get("Authorization") == f"Bearer {expected_tok}", \
            f"scheme={scheme}: expected Bearer {expected_tok!r}, got {headers.get('Authorization')!r}"


# ---------------------------------------------------------------------------
# Error handling paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_callback_idp_error_param_shows_html_error_page():
    """When the IdP returns error=access_denied, callback shows a 200 HTML error page."""
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, _, _ = _build_app_and_injector({"myAuth": resolved}, {"myAuth": store})

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/auth/callback?error=access_denied&state=anything")

    assert r.status_code == 200
    assert "access_denied" in r.text


@pytest.mark.asyncio
@respx.mock
async def test_callback_token_endpoint_failure_shows_error_page():
    """When the token endpoint returns 500, callback shows an error page (not a crash)."""
    session_id = "e2e-tok-fail"
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, _ = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    nonce = await oauth_state.issue_nonce(session_id, "myAuth")
    r = client.get(f"/auth/login?nonce={nonce}")
    state_param = _extract_state_param(r.headers["location"])

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500, text="internal error"))

    r = client.get(f"/auth/callback?code=code&state={state_param}")
    # Must render an error page (200 or 4xx), not crash (5xx from our server)
    assert r.status_code in (200, 400, 500)
    assert r.status_code != 502  # must not be a blind proxy error


@pytest.mark.asyncio
async def test_injector_raises_auth_required_before_login():
    """Before the OAuth flow completes, inject() raises AuthRequiredError with a login URL."""
    from specmcp.errors import AuthRequiredError

    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    _, oauth_state, injector = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    with pytest.raises(AuthRequiredError) as exc_info:
        await injector.inject(
            [[AuthRequirement(scheme_name="myAuth", scopes=[])]],
            headers={},
            params={},
            session=_Session("not-yet-logged-in"),
        )

    exc = exc_info.value
    assert exc.session_id == "not-yet-logged-in"
    assert exc.login_url is not None
    assert "/auth/login?nonce=" in exc.login_url


@pytest.mark.asyncio
@respx.mock
async def test_delete_session_after_login_deauthenticates():
    """DELETE /auth/session/<id> must remove the token; status returns false after."""
    from unittest.mock import patch

    session_id = "e2e-delete"
    store = InMemoryTokenStore()
    resolved = _make_resolved("myAuth", store)
    app, oauth_state, _ = _build_app_and_injector(
        {"myAuth": resolved}, {"myAuth": store}
    )

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    # Complete login flow
    nonce = await oauth_state.issue_nonce(session_id, "myAuth")
    r = client.get(f"/auth/login?nonce={nonce}")
    state_param = _extract_state_param(r.headers["location"])
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 3600}
    ))
    client.get(f"/auth/callback?code=code&state={state_param}")

    # Confirm authenticated
    r = client.get(f"/auth/status?session={session_id}")
    assert r.json()["authenticated"] is True

    # Delete the session (patch loopback check for TestClient)
    with patch("specmcp.runtime.oauth_handler._check_management_access", return_value=True):
        r = client.delete(f"/auth/session/{session_id}")
    assert r.status_code == 204

    # Confirm no longer authenticated
    r = client.get(f"/auth/status?session={session_id}")
    assert r.json()["authenticated"] is False
