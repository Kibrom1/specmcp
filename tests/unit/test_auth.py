"""Unit tests for specmcp.auth.injector.AuthInjector."""

from __future__ import annotations

import os
import textwrap
import tempfile

import pytest

from specmcp.auth.injector import AuthInjector
from specmcp.config import (
    ApiKeyAuthConfig,
    BearerAuthConfig,
    Config,
    SensitiveStr,
)
from specmcp.core.model import AuthRequirement
from specmcp.errors import AuthConfigError


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_api_key_config(in_: str = "header", name: str = "X-Api-Key") -> ApiKeyAuthConfig:
    return ApiKeyAuthConfig.model_validate({
        "type": "apiKey",
        "in": in_,
        "name": name,
        "value_from": "env(DUMMY)",  # value_from field — not resolved here
    })


def _make_bearer_config() -> BearerAuthConfig:
    return BearerAuthConfig.model_validate({
        "type": "bearer",
        "value_from": "env(DUMMY)",
    })


def _injector_with_schemes(**schemes) -> AuthInjector:
    """Build an injector directly from {name: ResolvedScheme} dict."""
    from specmcp.auth.injector import ResolvedScheme
    resolved = {
        name: ResolvedScheme(
            scheme_name=name,
            config=cfg,
            credential=SensitiveStr(token),
        )
        for name, (cfg, token) in schemes.items()
    }
    return AuthInjector(_schemes=resolved)


def _auth_reqs(*names: str) -> list[list[AuthRequirement]]:
    """Single OR-group containing all given scheme names (AND)."""
    return [[AuthRequirement(scheme_name=n) for n in names]]


def _auth_reqs_or(*groups: tuple[str, ...]) -> list[list[AuthRequirement]]:
    """Multiple OR-groups."""
    return [[AuthRequirement(scheme_name=n) for n in group] for group in groups]


# ---------------------------------------------------------------------------
# AuthInjector.build
# ---------------------------------------------------------------------------


def test_build_no_config_returns_empty_injector():
    injector = AuthInjector.build(None)
    assert injector.configured_schemes == frozenset()


def test_build_with_config_resolves_env_vars(monkeypatch):
    monkeypatch.setenv("PETSTORE_API_KEY", "secret-value")
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          petstoreApiKey:
            type: apiKey
            in: header
            name: X-Api-Key
            value_from: env(PETSTORE_API_KEY)
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(cfg_yaml)
        cfg_path = f.name

    cfg = Config.load(cfg_path)
    injector = AuthInjector.build(cfg)
    assert "petstoreApiKey" in injector.configured_schemes
    assert injector.has_scheme("petstoreApiKey")


# ---------------------------------------------------------------------------
# No auth required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_no_auth_returns_unchanged():
    injector = AuthInjector.build(None)
    h, p = await injector.inject([], headers={"Accept": "application/json"}, params={"limit": "10"})
    assert h == {"Accept": "application/json"}
    assert p == {"limit": "10"}


@pytest.mark.asyncio
async def test_inject_no_auth_does_not_mutate_originals():
    injector = AuthInjector.build(None)
    orig_h = {"Accept": "application/json"}
    orig_p = {"limit": "10"}
    h, p = await injector.inject([], headers=orig_h, params=orig_p)
    assert orig_h is not h
    assert orig_p is not p


# ---------------------------------------------------------------------------
# apiKey in header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_api_key_header():
    injector = _injector_with_schemes(
        myKey=(_make_api_key_config(in_="header", name="X-Api-Key"), "tok123")
    )
    h, p = await injector.inject(_auth_reqs("myKey"), headers={}, params={})
    assert h["X-Api-Key"] == "tok123"
    assert p == {}


@pytest.mark.asyncio
async def test_inject_api_key_header_preserves_existing():
    injector = _injector_with_schemes(
        myKey=(_make_api_key_config(in_="header", name="X-Api-Key"), "tok123")
    )
    h, p = await injector.inject(_auth_reqs("myKey"), headers={"Content-Type": "application/json"}, params={})
    assert h["Content-Type"] == "application/json"
    assert h["X-Api-Key"] == "tok123"


# ---------------------------------------------------------------------------
# apiKey in query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_api_key_query():
    injector = _injector_with_schemes(
        myKey=(_make_api_key_config(in_="query", name="api_key"), "qsecret")
    )
    h, p = await injector.inject(_auth_reqs("myKey"), headers={}, params={"limit": "5"})
    assert p["api_key"] == "qsecret"
    assert p["limit"] == "5"
    assert h == {}


# ---------------------------------------------------------------------------
# apiKey in cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_api_key_cookie_no_existing():
    injector = _injector_with_schemes(
        myKey=(_make_api_key_config(in_="cookie", name="session"), "csecret")
    )
    h, p = await injector.inject(_auth_reqs("myKey"), headers={}, params={})
    assert h["Cookie"] == "session=csecret"


@pytest.mark.asyncio
async def test_inject_api_key_cookie_merges_existing():
    injector = _injector_with_schemes(
        myKey=(_make_api_key_config(in_="cookie", name="session"), "csecret")
    )
    h, p = await injector.inject(_auth_reqs("myKey"), headers={"Cookie": "other=val"}, params={})
    assert "session=csecret" in h["Cookie"]
    assert "other=val" in h["Cookie"]


# ---------------------------------------------------------------------------
# Bearer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_bearer_adds_authorization_header():
    injector = _injector_with_schemes(
        myBearer=(_make_bearer_config(), "mytoken")
    )
    h, p = await injector.inject(_auth_reqs("myBearer"), headers={}, params={})
    assert h["Authorization"] == "Bearer mytoken"


@pytest.mark.asyncio
async def test_inject_bearer_does_not_add_query_params():
    injector = _injector_with_schemes(
        myBearer=(_make_bearer_config(), "mytoken")
    )
    h, p = await injector.inject(_auth_reqs("myBearer"), headers={}, params={})
    assert p == {}


# ---------------------------------------------------------------------------
# OR-group selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_picks_first_satisfied_or_group():
    """If the first OR-group is satisfied, it's used."""
    injector = _injector_with_schemes(
        schemeA=(_make_api_key_config(in_="header", name="X-A"), "valA"),
        schemeB=(_make_api_key_config(in_="header", name="X-B"), "valB"),
    )
    # Two OR-groups: [A] or [B]. First is A.
    reqs = _auth_reqs_or(("schemeA",), ("schemeB",))
    h, p = await injector.inject(reqs, headers={}, params={})
    assert h.get("X-A") == "valA"
    assert "X-B" not in h


@pytest.mark.asyncio
async def test_inject_falls_through_to_second_or_group():
    """If the first OR-group is not satisfied, the second is tried."""
    injector = _injector_with_schemes(
        schemeB=(_make_api_key_config(in_="header", name="X-B"), "valB"),
        # schemeA is NOT configured
    )
    reqs = _auth_reqs_or(("schemeA",), ("schemeB",))
    h, p = await injector.inject(reqs, headers={}, params={})
    assert h.get("X-B") == "valB"
    assert "X-A" not in h


@pytest.mark.asyncio
async def test_inject_and_group_applies_all_schemes():
    """Both schemes in an AND-group must be injected."""
    injector = _injector_with_schemes(
        schemeA=(_make_api_key_config(in_="header", name="X-A"), "valA"),
        schemeB=(_make_api_key_config(in_="query", name="b_key"), "valB"),
    )
    reqs = [
        [AuthRequirement(scheme_name="schemeA"), AuthRequirement(scheme_name="schemeB")]
    ]
    h, p = await injector.inject(reqs, headers={}, params={})
    assert h["X-A"] == "valA"
    assert p["b_key"] == "valB"


# ---------------------------------------------------------------------------
# Missing scheme → AuthConfigError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_missing_scheme_raises():
    injector = AuthInjector.build(None)  # no schemes configured
    with pytest.raises(AuthConfigError) as exc_info:
        await injector.inject(_auth_reqs("myScheme"), headers={}, params={})
    assert "myScheme" in str(exc_info.value)


@pytest.mark.asyncio
async def test_inject_all_groups_missing_raises():
    injector = _injector_with_schemes(
        schemeC=(_make_api_key_config(), "valC"),
    )
    reqs = _auth_reqs_or(("schemeA",), ("schemeB",))  # neither A nor B configured
    with pytest.raises(AuthConfigError):
        await injector.inject(reqs, headers={}, params={})


# ---------------------------------------------------------------------------
# SensitiveStr does not leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_does_not_appear_in_exception_message():
    """Auth errors must never contain the credential value."""
    from specmcp.auth.injector import ResolvedScheme
    scheme = ResolvedScheme(
        scheme_name="secret",
        config=_make_api_key_config(),
        credential=SensitiveStr("SUPER_SECRET_VALUE"),
    )
    injector = AuthInjector(_schemes={"secret": scheme})
    # Force a missing-scheme error (references a DIFFERENT scheme)
    try:
        await injector.inject(_auth_reqs("missing"), headers={}, params={})
    except AuthConfigError as exc:
        assert "SUPER_SECRET_VALUE" not in str(exc)


def test_sensitive_str_not_in_repr():
    from specmcp.auth.injector import ResolvedScheme
    scheme = ResolvedScheme(
        scheme_name="s",
        config=_make_api_key_config(),
        credential=SensitiveStr("LEAK_ME"),
    )
    assert "LEAK_ME" not in repr(scheme)
    assert "LEAK_ME" not in str(scheme)


# ---------------------------------------------------------------------------
# has_scheme / configured_schemes
# ---------------------------------------------------------------------------


def test_has_scheme_true():
    injector = _injector_with_schemes(foo=(_make_api_key_config(), "v"))
    assert injector.has_scheme("foo") is True


def test_has_scheme_false():
    injector = _injector_with_schemes(foo=(_make_api_key_config(), "v"))
    assert injector.has_scheme("bar") is False


def test_configured_schemes_returns_frozenset():
    injector = _injector_with_schemes(
        a=(_make_api_key_config(), "v1"),
        b=(_make_bearer_config(), "v2"),
    )
    assert injector.configured_schemes == frozenset({"a", "b"})
