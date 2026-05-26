"""Unit tests for specmcp.auth.oauth2_authcode.AuthCodeHandler."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from specmcp.auth.oauth2_authcode import AuthCodeHandler
from specmcp.auth.token_store import InMemoryTokenStore, OAuthTokens
from specmcp.config import OAuth2AuthorizationCodeConfig, SensitiveStr
from specmcp.errors import AuthConfigError, AuthRequiredError, TokenRefreshError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TOKEN_URL = "https://auth.example.com/oauth/token"
LOGIN_BASE = "http://localhost:8765"
CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"


def _make_cfg() -> OAuth2AuthorizationCodeConfig:
    return OAuth2AuthorizationCodeConfig(
        type="oauth2_authorization_code",
        authorization_url="https://auth.example.com/authorize",
        token_url=TOKEN_URL,
        client_id_from="env(CLIENT_ID)",
        client_secret_from="env(CLIENT_SECRET)",
        redirect_uri="http://localhost:8765/auth/callback",
        scopes=["openid", "read"],
    )


async def _noop_issue_nonce(session_id: str, scheme_name: str) -> str:
    return f"nonce-for-{session_id}"


def _make_handler(
    token_store: InMemoryTokenStore | None = None,
    issue_nonce=None,
) -> AuthCodeHandler:
    return AuthCodeHandler(
        scheme_name="myAuth",
        config=_make_cfg(),
        client_id=SensitiveStr(CLIENT_ID),
        client_secret=SensitiveStr(CLIENT_SECRET),
        token_store=token_store or InMemoryTokenStore(),
        issue_nonce=issue_nonce or _noop_issue_nonce,
        login_base_url=LOGIN_BASE,
    )


class _FakeSession:
    def __init__(self, session_id: str = "test-session"):
        self.session_id = session_id
        self.client_token = None


# ---------------------------------------------------------------------------
# apply() — no session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_no_session_raises_auth_config_error():
    handler = _make_handler()
    with pytest.raises(AuthConfigError, match="requires an active session"):
        await handler.apply({}, {}, session=None)


@pytest.mark.asyncio
async def test_apply_empty_session_id_raises_auth_config_error():
    class _EmptySession:
        session_id = ""
        client_token = None

    handler = _make_handler()
    with pytest.raises(AuthConfigError):
        await handler.apply({}, {}, session=_EmptySession())


# ---------------------------------------------------------------------------
# apply() — valid token in store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_valid_token_injects_bearer():
    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("valid-access-token"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("sess1", tokens)

    handler = _make_handler(token_store=store)
    headers: dict = {}
    await handler.apply(headers, {}, session=_FakeSession("sess1"))

    assert headers.get("Authorization") == "Bearer valid-access-token"


@pytest.mark.asyncio
async def test_apply_valid_token_does_not_call_token_endpoint():
    """No network call should happen when a valid token is already stored."""
    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("tok"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("sess2", tokens)

    handler = _make_handler(token_store=store)
    # If it tries to call the token endpoint, respx would raise an error
    with respx.mock(assert_all_called=False):
        await handler.apply({}, {}, session=_FakeSession("sess2"))
    # No exception → no network call


# ---------------------------------------------------------------------------
# apply() — no token → raises AuthRequiredError with login URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_no_token_raises_auth_required_error():
    handler = _make_handler()
    with pytest.raises(AuthRequiredError) as exc_info:
        await handler.apply({}, {}, session=_FakeSession("no-token-session"))

    exc = exc_info.value
    assert exc.session_id == "no-token-session"
    assert exc.login_url is not None
    assert "/auth/login?nonce=" in exc.login_url


@pytest.mark.asyncio
async def test_apply_no_token_login_url_uses_base_url():
    handler = _make_handler()
    with pytest.raises(AuthRequiredError) as exc_info:
        await handler.apply({}, {}, session=_FakeSession("sess"))

    assert exc_info.value.login_url.startswith(LOGIN_BASE)


@pytest.mark.asyncio
async def test_apply_nonce_embedded_in_login_url():
    """The nonce returned by issue_nonce must appear in the login URL."""
    nonce_issued = []

    async def _tracking_issue_nonce(session_id: str, scheme_name: str) -> str:
        nonce = f"fixed-nonce-{session_id}"
        nonce_issued.append(nonce)
        return nonce

    handler = _make_handler(issue_nonce=_tracking_issue_nonce)
    with pytest.raises(AuthRequiredError) as exc_info:
        await handler.apply({}, {}, session=_FakeSession("sess-nonce"))

    assert len(nonce_issued) == 1
    assert nonce_issued[0] in exc_info.value.login_url


# ---------------------------------------------------------------------------
# apply() — expired token + refresh token → silent refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_apply_expired_token_triggers_refresh():
    """Expired token with refresh_token should silently refresh."""
    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old-access"),
        refresh_token=SensitiveStr("the-refresh-token"),
        expires_at=time.time() - 10,  # already expired
    )
    await store.save("sess-refresh", expired)

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "new-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    ))

    handler = _make_handler(token_store=store)
    headers: dict = {}
    await handler.apply(headers, {}, session=_FakeSession("sess-refresh"))

    assert headers.get("Authorization") == "Bearer new-access-token"


@pytest.mark.asyncio
@respx.mock
async def test_apply_refresh_stores_new_tokens():
    """After a successful refresh the new tokens must be saved to the store."""
    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )
    await store.save("sess-store", expired)

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "new-tok", "expires_in": 3600},
    ))

    handler = _make_handler(token_store=store)
    await handler.apply({}, {}, session=_FakeSession("sess-store"))

    saved = await store.get("sess-store")
    assert saved is not None
    assert saved.access_token.reveal() == "new-tok"


@pytest.mark.asyncio
@respx.mock
async def test_apply_refresh_uses_basic_auth():
    """Token refresh must use HTTP Basic Auth, not body params."""
    import base64

    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )
    await store.save("sess-basic", expired)

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "new", "expires_in": 3600}
    ))

    handler = _make_handler(token_store=store)
    await handler.apply({}, {}, session=_FakeSession("sess-basic"))

    req = route.calls[0].request
    auth_header = req.headers.get("authorization", "")
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header[6:]).decode()
    assert decoded == f"{CLIENT_ID}:{CLIENT_SECRET}"


@pytest.mark.asyncio
@respx.mock
async def test_apply_refresh_sends_refresh_token_in_body():
    """The refresh_token value must be sent in the POST body."""
    import urllib.parse

    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("my-refresh-token"),
        expires_at=time.time() - 10,
    )
    await store.save("sess-body", expired)

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "new", "expires_in": 3600}
    ))

    handler = _make_handler(token_store=store)
    await handler.apply({}, {}, session=_FakeSession("sess-body"))

    req = route.calls[0].request
    body = dict(
        pair.split("=", 1)
        for pair in req.content.decode().split("&")
        if "=" in pair
    )
    body = {urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v) for k, v in body.items()}
    assert body.get("grant_type") == "refresh_token"
    assert body.get("refresh_token") == "my-refresh-token"
    assert "client_id" not in body
    assert "client_secret" not in body


@pytest.mark.asyncio
@respx.mock
async def test_apply_refresh_preserves_existing_refresh_token():
    """If the token endpoint does not return a new refresh_token, keep the old one."""
    store = InMemoryTokenStore()
    old_rt = "old-refresh-token"
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr(old_rt),
        expires_at=time.time() - 10,
    )
    await store.save("sess-rt-keep", expired)

    # Token endpoint returns access_token but no refresh_token
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "new", "expires_in": 3600}
    ))

    handler = _make_handler(token_store=store)
    await handler.apply({}, {}, session=_FakeSession("sess-rt-keep"))

    saved = await store.get("sess-rt-keep")
    assert saved is not None
    assert saved.refresh_token is not None
    assert saved.refresh_token.reveal() == old_rt


# ---------------------------------------------------------------------------
# apply() — expired token + refresh fails → AuthRequiredError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_apply_refresh_fails_raises_auth_required():
    """If the refresh call fails, fall back to AuthRequiredError with login URL."""
    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("dead-rt"),
        expires_at=time.time() - 10,
    )
    await store.save("sess-fail", expired)

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    handler = _make_handler(token_store=store)
    with pytest.raises(AuthRequiredError) as exc_info:
        await handler.apply({}, {}, session=_FakeSession("sess-fail"))

    assert "/auth/login?nonce=" in (exc_info.value.login_url or "")


# ---------------------------------------------------------------------------
# _do_refresh — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_do_refresh_network_error_raises_token_refresh_error():
    store = InMemoryTokenStore()
    handler = _make_handler(token_store=store)

    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )

    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("no host"))

    with pytest.raises(TokenRefreshError, match="token endpoint"):
        await handler._do_refresh(expired)


@pytest.mark.asyncio
@respx.mock
async def test_do_refresh_non_json_response_raises_token_refresh_error():
    store = InMemoryTokenStore()
    handler = _make_handler(token_store=store)
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(TokenRefreshError, match="non-JSON"):
        await handler._do_refresh(expired)


@pytest.mark.asyncio
@respx.mock
async def test_do_refresh_missing_access_token_raises_token_refresh_error():
    store = InMemoryTokenStore()
    handler = _make_handler(token_store=store)
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))
    with pytest.raises(TokenRefreshError, match="access_token"):
        await handler._do_refresh(expired)


# ---------------------------------------------------------------------------
# Concurrency: per-session lock prevents double-refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_apply_refreshes_token_only_once():
    """Two concurrent calls for the same session should result in only one
    refresh token exchange (the second call rides the updated store)."""
    import asyncio

    store = InMemoryTokenStore()
    expired = OAuthTokens(
        access_token=SensitiveStr("old"),
        refresh_token=SensitiveStr("rt"),
        expires_at=time.time() - 10,
    )
    await store.save("sess-concurrent", expired)

    call_count = 0

    async def _refresh_side_effect(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": f"new-{call_count}", "expires_in": 3600})

    respx.post(TOKEN_URL).mock(side_effect=_refresh_side_effect)

    handler = _make_handler(token_store=store)

    results = await asyncio.gather(
        handler.apply({}, {}, session=_FakeSession("sess-concurrent")),
        handler.apply({}, {}, session=_FakeSession("sess-concurrent")),
        return_exceptions=True,
    )

    # Both should succeed (not raise)
    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception: {r}"

    # The token endpoint should only be called once due to the per-session lock
    assert call_count == 1, f"Expected 1 refresh call, got {call_count}"


# ---------------------------------------------------------------------------
# AuthInjector integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injector_register_auth_code_handler_satisfies_group():
    """After registering an AuthCodeHandler, the injector considers the scheme configured."""
    from specmcp.auth.injector import AuthInjector
    from specmcp.core.model import AuthRequirement

    store = InMemoryTokenStore()
    tokens = OAuthTokens(
        access_token=SensitiveStr("injector-tok"),
        refresh_token=None,
        expires_at=time.time() + 3600,
    )
    await store.save("inj-sess", tokens)

    injector = AuthInjector.build(None)
    handler = _make_handler(token_store=store)
    injector.register_auth_code_handler("myAuth", handler)

    assert injector.has_scheme("myAuth")
    assert "myAuth" in injector.configured_schemes

    auth_req = [[AuthRequirement(scheme_name="myAuth", scopes=[])]]
    headers, params = await injector.inject(
        auth_req,
        headers={},
        params={},
        session=_FakeSession("inj-sess"),
    )
    assert headers.get("Authorization") == "Bearer injector-tok"


@pytest.mark.asyncio
async def test_injector_auth_code_no_handler_raises_auth_config_error():
    """Injecting an auth_code scheme with no registered handler raises AuthConfigError."""
    from specmcp.auth.injector import AuthInjector, ResolvedScheme
    from specmcp.core.model import AuthRequirement

    injector = AuthInjector.build(None)
    # Force-add a placeholder scheme with no registered handler
    injector._schemes["orphanAuth"] = ResolvedScheme(
        scheme_name="orphanAuth",
        config=_make_cfg(),
    )

    auth_req = [[AuthRequirement(scheme_name="orphanAuth", scopes=[])]]
    with pytest.raises(AuthConfigError, match="no registered AuthCodeHandler"):
        await injector.inject(auth_req, headers={}, params={}, session=_FakeSession())
