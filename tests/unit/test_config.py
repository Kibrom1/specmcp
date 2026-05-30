"""Unit tests for config schema and SensitiveStr."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from specmcp.config import (
    Config,
    SensitiveStr,
    _resolve_value_from,
    scheme_name_to_env_var,
)
from specmcp.errors import ConfigEnvVarError, ConfigError, ConfigVersionError


# ---------------------------------------------------------------------------
# scheme_name_to_env_var  (camelCase → UPPER_SNAKE_CASE — part of the public contract)
# ---------------------------------------------------------------------------


import pytest as _pytest

@_pytest.mark.parametrize("name,expected", [
    ("petstoreApiKey",  "PETSTORE_API_KEY"),
    ("myApiKey",        "MY_API_KEY"),
    ("bearerAuth",      "BEARER_AUTH"),
    ("stripe-api-key",  "STRIPE_API_KEY"),
    ("myScheme",        "MY_SCHEME"),
    ("simplekey",       "SIMPLEKEY"),
    # Single uppercase letter before a title word must NOT be split off.
    # 'myOAuth' → MY_OAUTH (not MY_O_AUTH); the 'O' stays attached to 'Auth'.
    ("myOAuth",         "MY_OAUTH"),
    ("myOAuthClient",   "MY_OAUTH_CLIENT"),
    # Run of 2+ uppercase letters before a title word IS split (HTMLParser → HTML_PARSER).
    ("HTMLParser",      "HTML_PARSER"),
    # Digits count as word-break triggers for the following uppercase letter.
    ("OAuth2Flow",      "OAUTH2_FLOW"),
])
def test_scheme_name_to_env_var(name: str, expected: str) -> None:
    assert scheme_name_to_env_var(name) == expected


# ---------------------------------------------------------------------------
# SensitiveStr
# ---------------------------------------------------------------------------


def test_sensitive_str_repr_is_redacted():
    s = SensitiveStr("super-secret-token")
    assert repr(s) == "<redacted>"
    assert str(s) == "<redacted>"


def test_sensitive_str_reveal():
    s = SensitiveStr("super-secret-token")
    assert s.reveal() == "super-secret-token"


def test_sensitive_str_not_in_format():
    s = SensitiveStr("my-secret")
    formatted = f"The value is {s}"
    assert "my-secret" not in formatted
    assert "<redacted>" in formatted


# ---------------------------------------------------------------------------
# _resolve_value_from
# ---------------------------------------------------------------------------


def test_resolve_value_from_reads_env(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk_test_abc123")
    result = _resolve_value_from("env(TEST_API_KEY)", "myScheme")
    assert isinstance(result, SensitiveStr)
    assert result.reveal() == "sk_test_abc123"
    # str() must not reveal the value
    assert str(result) == "<redacted>"


def test_resolve_value_from_missing_env(monkeypatch):
    monkeypatch.delenv("MISSING_KEY_XYZ", raising=False)
    with pytest.raises(ConfigEnvVarError) as exc_info:
        _resolve_value_from("env(MISSING_KEY_XYZ)", "myScheme")
    msg = exc_info.value.message
    assert "MISSING_KEY_XYZ" in msg         # var name is OK to show
    # The exception must not contain any value (it's missing, but just in case)


def test_resolve_value_from_bad_format():
    with pytest.raises(ConfigError):
        _resolve_value_from("not_env_format", "myScheme")


# ---------------------------------------------------------------------------
# Config.load — valid config
# ---------------------------------------------------------------------------


MINIMAL_CONFIG = textwrap.dedent("""\
    version: "1"
    spec:
      source: "./openapi.json"
""")


def test_load_minimal_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(MINIMAL_CONFIG)
    cfg = Config.load(cfg_file)
    assert cfg.version == "1"
    assert cfg.spec.source == "./openapi.json"
    assert cfg.spec.cache is True
    assert cfg.dispatch.default_timeout_seconds == 30.0


FULL_CONFIG = textwrap.dedent("""\
    version: "1"
    spec:
      source: "https://api.example.com/openapi.json"
      cache: false
    server:
      include_deprecated: false
      include_tags: [pets]
      exclude_operations: [deletePet]
    auth:
      petstoreApiKey:
        type: apiKey
        in: header
        name: X-API-Key
        value_from: env(PETSTORE_API_KEY)
    dispatch:
      default_timeout_seconds: 60
      per_host_concurrency: 5
    simplify:
      collapse_unions: false
    transport:
      default: stdio
    telemetry:
      enabled: false
    logging:
      level: debug
      format: text
""")


def test_load_full_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PETSTORE_API_KEY", "sk_test")
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(FULL_CONFIG)
    cfg = Config.load(cfg_file)
    assert cfg.server.include_deprecated is False
    assert cfg.server.include_tags == ["pets"]
    assert cfg.dispatch.default_timeout_seconds == 60.0
    assert cfg.simplify.collapse_unions is False
    assert cfg.logging.level == "debug"


# ---------------------------------------------------------------------------
# Config.load — errors
# ---------------------------------------------------------------------------


def test_load_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        Config.load("/tmp/definitely_does_not_exist_specmcp.yaml")


def test_load_wrong_version(tmp_path):
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text('version: "99"\nspec:\n  source: "./openapi.json"\n')
    with pytest.raises(ConfigVersionError):
        Config.load(cfg_file)


def test_load_missing_version(tmp_path):
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text("spec:\n  source: ./openapi.json\n")
    with pytest.raises((ConfigVersionError, ConfigError)):
        Config.load(cfg_file)


def test_load_not_a_mapping(tmp_path):
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text("- item1\n- item2\n")
    with pytest.raises(ConfigError):
        Config.load(cfg_file)


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


def test_resolve_auth_values_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("PETSTORE_API_KEY", "sk_live_xyz")
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(FULL_CONFIG)
    cfg = Config.load(cfg_file)
    resolved = cfg.resolve_auth_values()
    assert "petstoreApiKey" in resolved
    assert resolved["petstoreApiKey"].reveal() == "sk_live_xyz"
    assert str(resolved["petstoreApiKey"]) == "<redacted>"


def test_resolve_auth_values_missing_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PETSTORE_API_KEY", raising=False)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(FULL_CONFIG)
    cfg = Config.load(cfg_file)
    with pytest.raises(ConfigEnvVarError) as exc_info:
        cfg.resolve_auth_values()
    # Must include the var name, must NOT include any value
    assert "PETSTORE_API_KEY" in exc_info.value.message


def test_auth_value_not_in_exception_message(tmp_path, monkeypatch):
    """The actual secret value must never appear in any exception message."""
    monkeypatch.setenv("PETSTORE_API_KEY", "SUPER_SECRET_12345")
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(FULL_CONFIG)
    cfg = Config.load(cfg_file)
    resolved = cfg.resolve_auth_values()
    # Create an exception and confirm the value isn't in it
    try:
        raise ConfigError("something went wrong", context={"scheme": "petstoreApiKey"})
    except ConfigError as exc:
        assert "SUPER_SECRET_12345" not in str(exc)
        assert "SUPER_SECRET_12345" not in repr(exc)


# ---------------------------------------------------------------------------
# Config.scaffold
# ---------------------------------------------------------------------------


def test_scaffold_basic():
    yaml_str = Config.scaffold(
        spec_source="https://api.example.com/openapi.json",
        auth_schemes=[],
    )
    assert 'version: "1"' in yaml_str
    assert "source:" in yaml_str
    assert "api.example.com" in yaml_str


def test_scaffold_with_apikey_scheme():
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "myApiKey",
            "type": "apiKey",
            "in": "header",
            "header_name": "X-My-Key",
        }],
    )
    assert "myApiKey:" in yaml_str
    assert "type: apiKey" in yaml_str
    assert "env(MY_API_KEY)" in yaml_str  # camelCase → UPPER_SNAKE_CASE


def test_scaffold_with_bearer_scheme():
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "bearerAuth",
            "type": "http",
            "scheme": "bearer",
        }],
    )
    assert "bearerAuth:" in yaml_str
    assert "type: bearer" in yaml_str


def test_scaffold_oauth2_scheme_emits_client_credentials_block():
    """oauth2_client_credentials type emits a full client credentials block."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "oauth2Flow",
            "type": "oauth2_client_credentials",
            "token_url": "https://auth.example.com/oauth/token",
        }],
    )
    assert "oauth2Flow:" in yaml_str
    assert "type: oauth2_client_credentials" in yaml_str
    assert "client_id_from: env(OAUTH2_FLOW_CLIENT_ID)" in yaml_str
    assert "client_secret_from: env(OAUTH2_FLOW_CLIENT_SECRET)" in yaml_str


def test_scaffold_oauth2_authorization_code_block():
    """oauth2_authorization_code type emits a full auth code block with version: 2."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "myAuth",
            "type": "oauth2_authorization_code",
            "authorization_url": "https://auth.example.com/oauth/authorize",
            "token_url": "https://auth.example.com/oauth/token",
            "scopes": ["read", "write"],
        }],
    )
    assert 'version: "2"' in yaml_str
    assert "myAuth:" in yaml_str
    assert "type: oauth2_authorization_code" in yaml_str
    assert "authorization_url: https://auth.example.com/oauth/authorize" in yaml_str
    assert "token_url: https://auth.example.com/oauth/token" in yaml_str
    assert "redirect_uri: http://localhost:8765/auth/callback" in yaml_str
    assert "client_id_from: env(MY_AUTH_CLIENT_ID)" in yaml_str
    assert "client_secret_from: env(MY_AUTH_CLIENT_SECRET)" in yaml_str
    assert '"read"' in yaml_str
    assert '"write"' in yaml_str


def test_scaffold_oauth2_scheme_my_oauth_env_var_names():
    """myOAuth → MY_OAUTH (not MY_O_AUTH) — single uppercase before title word is kept."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "myOAuth",
            "type": "oauth2_client_credentials",
            "token_url": "https://auth.example.com/oauth/token",
        }],
    )
    assert "client_id_from: env(MY_OAUTH_CLIENT_ID)" in yaml_str
    assert "client_secret_from: env(MY_OAUTH_CLIENT_SECRET)" in yaml_str
    # Ensure the old buggy form is absent
    assert "MY_O_AUTH" not in yaml_str


def test_scaffold_non_auth_code_scheme_emits_version_1():
    """When no auth code schemes are present, version: 1 is emitted."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{"name": "apiKey", "type": "apiKey", "in": "header", "header_name": "X-Api-Key"}],
    )
    assert 'version: "1"' in yaml_str
    assert 'version: "2"' not in yaml_str


def test_scaffold_includes_streaming_fields():
    """The dispatch: section of the scaffold must include all SSE streaming config fields."""
    yaml_str = Config.scaffold(spec_source="./spec.json", auth_schemes=[])
    assert "enable_streaming:" in yaml_str
    assert "streaming_timeout_multiplier:" in yaml_str
    assert "streaming_max_bytes:" in yaml_str
    # The multiplier footgun should be documented inline
    assert "per-operation" in yaml_str


# ---------------------------------------------------------------------------
# OAuth validators — token_url must be https://
# ---------------------------------------------------------------------------


def test_oauth2_cc_rejects_http_token_url(tmp_path, monkeypatch):
    """oauth2_client_credentials token_url must use https://."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: http://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    with pytest.raises(ConfigError, match="https"):
        Config.load(cfg_file)


def test_oauth2_cc_accepts_https_token_url(tmp_path, monkeypatch):
    """oauth2_client_credentials accepts https:// token_url."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: https://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    cfg = Config.load(cfg_file)
    assert "myOAuth" in cfg._auth_schemes


def test_oauth2_cc_accepts_localhost_http(tmp_path, monkeypatch):
    """oauth2_client_credentials accepts http://localhost for local testing."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          localOAuth:
            type: oauth2_client_credentials
            token_url: http://localhost:8080/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    cfg = Config.load(cfg_file)
    assert "localOAuth" in cfg._auth_schemes


# ---------------------------------------------------------------------------
# extra_params — reserved field names rejected
# ---------------------------------------------------------------------------


def test_oauth2_cc_rejects_reserved_extra_params(tmp_path, monkeypatch):
    """extra_params must not include reserved OAuth field names."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: https://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            extra_params:
              grant_type: custom  # reserved!
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    with pytest.raises(ConfigError, match="reserved"):
        Config.load(cfg_file)


def test_oauth2_cc_accepts_non_reserved_extra_params(tmp_path, monkeypatch):
    """extra_params accepts non-reserved custom fields."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: https://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            extra_params:
              audience: https://api.example.com
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    cfg = Config.load(cfg_file)
    scheme = cfg._auth_schemes["myOAuth"]
    assert scheme.extra_params["audience"] == "https://api.example.com"


# ---------------------------------------------------------------------------
# oauth2_authorization_code — requires version "2"
# ---------------------------------------------------------------------------


def test_oauth2_auth_code_requires_version_2(tmp_path, monkeypatch):
    """oauth2_authorization_code raises ConfigError if config version is '1'."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
        auth:
          userAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            redirect_uri: https://app.example.com/callback
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    with pytest.raises(ConfigError, match="version.*2"):
        Config.load(cfg_file)


def test_oauth2_auth_code_accepted_with_version_2(tmp_path, monkeypatch):
    """oauth2_authorization_code is accepted when config version is '2'."""
    monkeypatch.setenv("MY_CLIENT_ID", "cid")
    monkeypatch.setenv("MY_CLIENT_SECRET", "csecret")

    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
        auth:
          userAuth:
            type: oauth2_authorization_code
            authorization_url: https://auth.example.com/authorize
            token_url: https://auth.example.com/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            redirect_uri: https://app.example.com/callback
            scopes:
              - openid
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)

    cfg = Config.load(cfg_file)
    assert "userAuth" in cfg._auth_schemes


# ---------------------------------------------------------------------------
# ManagementConfig defaults
# ---------------------------------------------------------------------------


def test_management_config_defaults(tmp_path):
    """Config.management has sensible defaults when not specified."""
    cfg_yaml = textwrap.dedent("""\
        version: "1"
        spec:
          source: ./spec.json
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)
    cfg = Config.load(cfg_file)
    assert cfg.management.bind == "loopback"
    assert cfg.management.port == 8766
    assert cfg.management.management_token_from is None


# ---------------------------------------------------------------------------
# Version "2" is supported
# ---------------------------------------------------------------------------


def test_version_2_is_accepted(tmp_path):
    """Config version '2' loads without error."""
    cfg_yaml = textwrap.dedent("""\
        version: "2"
        spec:
          source: ./spec.json
    """)
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(cfg_yaml)
    cfg = Config.load(cfg_file)
    assert cfg.version == "2"


# ---------------------------------------------------------------------------
# redirect_uri HTTPS validator (parity with token_url / authorization_url)
# ---------------------------------------------------------------------------

_OAUTH2_AUTHCODE_BASE = textwrap.dedent("""\
    version: "2"
    spec:
      source: ./spec.json
    auth:
      myOAuth:
        type: oauth2_authorization_code
        authorization_url: https://auth.example.com/authorize
        token_url: https://auth.example.com/token
        client_id_from: env(CID)
        client_secret_from: env(CSEC)
        redirect_uri: {redirect_uri}
""")


def test_redirect_uri_https_is_accepted(tmp_path):
    """redirect_uri with https:// is valid."""
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(_OAUTH2_AUTHCODE_BASE.format(
        redirect_uri="https://my-server.example.com/auth/callback"
    ))
    cfg = Config.load(cfg_file)
    assert "myOAuth" in cfg._auth_schemes  # noqa: SLF001


def test_redirect_uri_localhost_http_is_accepted(tmp_path):
    """redirect_uri with http://localhost is valid (local development)."""
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(_OAUTH2_AUTHCODE_BASE.format(
        redirect_uri="http://localhost:8765/auth/callback"
    ))
    cfg = Config.load(cfg_file)
    assert "myOAuth" in cfg._auth_schemes  # noqa: SLF001


def test_redirect_uri_127_http_is_accepted(tmp_path):
    """redirect_uri with http://127.0.0.1 is valid (local development)."""
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(_OAUTH2_AUTHCODE_BASE.format(
        redirect_uri="http://127.0.0.1:8765/auth/callback"
    ))
    cfg = Config.load(cfg_file)
    assert "myOAuth" in cfg._auth_schemes  # noqa: SLF001


def test_redirect_uri_non_localhost_http_is_rejected(tmp_path):
    """redirect_uri with http:// on a non-localhost host must be rejected.

    Auth codes in transit over plain HTTP can be intercepted by network
    observers. Only HTTPS or loopback (localhost/127.0.0.1) HTTP is safe.
    """
    cfg_file = tmp_path / "mcp.config.yaml"
    cfg_file.write_text(_OAUTH2_AUTHCODE_BASE.format(
        redirect_uri="http://public-server.example.com/auth/callback"
    ))
    with pytest.raises(Exception):  # ConfigError wraps pydantic ValidationError
        Config.load(cfg_file)
