"""
Unit tests for OAuth 2.0 client_credentials flow in AuthInjector.

Tests:
  - build() resolves client_id / client_secret from env; creates a TokenCache per scheme
  - _fetch_token() happy path: token endpoint returns 200 with access_token + expires_in
  - _fetch_token() error: token endpoint returns non-200 → TokenRefreshError
  - _fetch_token() error: response body is not JSON → TokenRefreshError
  - _fetch_token() error: access_token missing from response → TokenRefreshError
  - _fetch_token() error: network error → TokenRefreshError
  - inject() with OAuth scheme: injects Bearer token into Authorization header
  - inject() caches token — token endpoint called only once on second inject()
  - inject() with expired token: re-fetches
  - build() with missing env var: raises ConfigEnvVarError
"""

from __future__ import annotations

import time
import textwrap
import tempfile

import pytest
import respx
import httpx

from specmcp.auth.injector import AuthInjector, ResolvedScheme
from specmcp.auth.token_cache import CachedToken, TokenCache
from specmcp.config import (
    Config,
    OAuth2ClientCredentialsConfig,
    SensitiveStr,
)
from specmcp.core.model import AuthRequirement
from specmcp.errors import AuthConfigError, ConfigEnvVarError, TokenRefreshError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN_URL = "https://auth.example.com/oauth/token"


def _make_oauth_config(
    token_url: str = TOKEN_URL,
    scopes: list[str] | None = None,
    extra_params: dict[str, str] | None = None,
) -> OAuth2ClientCredentialsConfig:
    return OAuth2ClientCredentialsConfig(
        type="oauth2_client_credentials",
        token_url=token_url,
        client_id_from="env(OAUTH_CLIENT_ID)",
        client_secret_from="env(OAUTH_CLIENT_SECRET)",
        scopes=scopes or [],
        extra_params=extra_params or {},
    )


def _oauth_injector(
    scheme_name: str = "myOAuth",
    token_url: str = TOKEN_URL,
    scopes: list[str] | None = None,
    extra_params: dict[str, str] | None = None,
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
) -> AuthInjector:
    """Build an AuthInjector with one OAuth scheme already wired up."""
    cfg = _make_oauth_config(token_url=token_url, scopes=scopes, extra_params=extra_params)
    resolved = ResolvedScheme(
        scheme_name=scheme_name,
        config=cfg,
        oauth_client_id=SensitiveStr(client_id),
        oauth_client_secret=SensitiveStr(client_secret),
    )
    return AuthInjector(
        _schemes={scheme_name: resolved},
        _token_caches={scheme_name: TokenCache()},
    )


def _auth_reqs(scheme_name: str) -> list[list[AuthRequirement]]:
    return [[AuthRequirement(scheme_name=scheme_name)]]


# ---------------------------------------------------------------------------
# build() — env var resolution
# ---------------------------------------------------------------------------


def test_build_resolves_oauth_credentials(monkeypatch):
    """build() should resolve client_id/secret from env and create a TokenCache."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent(f"""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: {TOKEN_URL}
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            scopes:
              - read
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    injector = AuthInjector.build(cfg)

    assert "myOAuth" in injector.configured_schemes
    assert "myOAuth" in injector._token_caches
    resolved = injector._schemes["myOAuth"]
    assert resolved.oauth_client_id is not None
    assert resolved.oauth_client_secret is not None
    # SensitiveStr — actual values must not leak through str()
    assert "cid" not in str(resolved.oauth_client_id)
    assert "csecret" not in str(resolved.oauth_client_secret)
    # But reveal() returns the real value
    assert resolved.oauth_client_id.reveal() == "cid"
    assert resolved.oauth_client_secret.reveal() == "csecret"


def test_build_raises_on_missing_client_id_env_var(monkeypatch):
    """build() should raise ConfigEnvVarError if client_id env var is absent."""
    monkeypatch.delenv("MISSING_CLIENT_ID", raising=False)
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent(f"""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: {TOKEN_URL}
            client_id_from: env(MISSING_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    with pytest.raises(ConfigEnvVarError, match="MISSING_CLIENT_ID"):
        AuthInjector.build(cfg)


# ---------------------------------------------------------------------------
# _fetch_token() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_success():
    """_fetch_token() returns a CachedToken on 200 with valid body."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "at-abc123", "token_type": "Bearer", "expires_in": 3600},
    ))

    token = await injector._fetch_token(resolved, cfg)

    assert isinstance(token, CachedToken)
    assert token.access_token == "at-abc123"
    assert token.expires_at > time.monotonic()
    assert not token.is_expired()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_sends_correct_form_fields():
    """_fetch_token() must POST grant_type, client_id, client_secret."""
    injector = _oauth_injector(client_id="my-id", client_secret="my-secret")
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok", "expires_in": 600},
    ))

    await injector._fetch_token(resolved, cfg)

    request = route.calls[0].request
    body = dict(pair.split("=") for pair in request.content.decode().split("&"))
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "my-id"
    assert body["client_secret"] == "my-secret"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_includes_scopes():
    """_fetch_token() must include 'scope' field when scopes are configured."""
    injector = _oauth_injector(scopes=["read", "write"])
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok", "expires_in": 600},
    ))

    await injector._fetch_token(resolved, cfg)

    request = route.calls[0].request
    body = dict(pair.split("=") for pair in request.content.decode().split("&"))
    assert body.get("scope") == "read+write"  # urllib-encoded space


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_includes_extra_params():
    """_fetch_token() must include extra_params in the POST body."""
    injector = _oauth_injector(extra_params={"audience": "https://api.example.com"})
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok", "expires_in": 600},
    ))

    await injector._fetch_token(resolved, cfg)

    request = route.calls[0].request
    raw_body = request.content.decode()
    assert "audience=" in raw_body


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_defaults_expires_in_to_3600():
    """If expires_in is absent, _fetch_token() defaults to 3600 seconds."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "tok"},  # no expires_in
    ))

    before = time.monotonic()
    token = await injector._fetch_token(resolved, cfg)
    after = time.monotonic()

    # expires_at should be ~3600 seconds from now
    assert 3590 < token.expires_at - before < 3610 + (after - before)


# ---------------------------------------------------------------------------
# _fetch_token() — error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_non_200_raises():
    """_fetch_token() raises TokenRefreshError for non-200 responses."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_client"}))

    with pytest.raises(TokenRefreshError) as exc_info:
        await injector._fetch_token(resolved, cfg)

    assert "400" in str(exc_info.value)
    # Token URL is safe to include; client_secret must not appear
    assert "my-secret" not in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_non_json_raises():
    """_fetch_token() raises TokenRefreshError if the body is not JSON."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text="not-json"))

    with pytest.raises(TokenRefreshError, match="non-JSON"):
        await injector._fetch_token(resolved, cfg)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_missing_access_token_raises():
    """_fetch_token() raises TokenRefreshError if access_token is absent."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"token_type": "Bearer"},  # no access_token
    ))

    with pytest.raises(TokenRefreshError, match="access_token"):
        await injector._fetch_token(resolved, cfg)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_network_error_raises():
    """_fetch_token() raises TokenRefreshError on network-level errors."""
    injector = _oauth_injector()
    resolved = injector._schemes["myOAuth"]
    cfg = resolved.config

    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TokenRefreshError, match="ConnectError"):
        await injector._fetch_token(resolved, cfg)


# ---------------------------------------------------------------------------
# inject() — full OAuth flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_inject_oauth_adds_bearer_header():
    """inject() with OAuth scheme should add Authorization: Bearer <token>."""
    injector = _oauth_injector()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "live-token", "expires_in": 3600},
    ))

    h, p = await injector.inject(_auth_reqs("myOAuth"), headers={}, params={})

    assert h["Authorization"] == "Bearer live-token"
    assert p == {}


@pytest.mark.asyncio
@respx.mock
async def test_inject_oauth_caches_token():
    """Token endpoint must be called exactly once across multiple inject() calls."""
    injector = _oauth_injector()

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "cached-token", "expires_in": 3600},
    ))

    h1, _ = await injector.inject(_auth_reqs("myOAuth"), headers={}, params={})
    h2, _ = await injector.inject(_auth_reqs("myOAuth"), headers={}, params={})

    assert h1["Authorization"] == "Bearer cached-token"
    assert h2["Authorization"] == "Bearer cached-token"
    assert route.call_count == 1, f"Expected 1 token fetch, got {route.call_count}"


@pytest.mark.asyncio
@respx.mock
async def test_inject_oauth_refreshes_expired_token():
    """inject() must re-fetch when the cached token is expired."""
    injector = _oauth_injector()

    # Pre-load an already-expired token
    injector._token_caches["myOAuth"]._token = CachedToken(
        access_token="old-token",
        expires_at=time.monotonic() - 1,  # already expired
    )

    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={"access_token": "fresh-token", "expires_in": 3600},
    ))

    h, _ = await injector.inject(_auth_reqs("myOAuth"), headers={}, params={})

    assert h["Authorization"] == "Bearer fresh-token"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_inject_oauth_token_refresh_error_propagates():
    """TokenRefreshError from the token endpoint must propagate through inject()."""
    injector = _oauth_injector()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    with pytest.raises(TokenRefreshError):
        await injector.inject(_auth_reqs("myOAuth"), headers={}, params={})

    # Cache must remain empty after a failed fetch
    assert injector._token_caches["myOAuth"]._token is None


@pytest.mark.asyncio
async def test_inject_unconfigured_oauth_raises_auth_config_error():
    """inject() with an unconfigured OAuth scheme raises AuthConfigError, not TokenRefreshError."""
    injector = AuthInjector.build(None)  # no schemes

    with pytest.raises(AuthConfigError, match="missingOAuth"):
        await injector.inject(_auth_reqs("missingOAuth"), headers={}, params={})
