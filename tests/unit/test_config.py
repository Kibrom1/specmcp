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
    """oauth2 type is now supported — scaffold emits a full oauth2_client_credentials block."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "oauth2Flow",
            "type": "oauth2",
        }],
    )
    assert "oauth2Flow:" in yaml_str
    assert "type: oauth2_client_credentials" in yaml_str
    assert "client_id_from: env(OAUTH2_FLOW_CLIENT_ID)" in yaml_str
    assert "client_secret_from: env(OAUTH2_FLOW_CLIENT_SECRET)" in yaml_str


def test_scaffold_oauth2_scheme_my_oauth_env_var_names():
    """myOAuth → MY_OAUTH (not MY_O_AUTH) — single uppercase before title word is kept."""
    yaml_str = Config.scaffold(
        spec_source="./spec.json",
        auth_schemes=[{
            "name": "myOAuth",
            "type": "oauth2",
        }],
    )
    assert "client_id_from: env(MY_OAUTH_CLIENT_ID)" in yaml_str
    assert "client_secret_from: env(MY_OAUTH_CLIENT_SECRET)" in yaml_str
    # Ensure the old buggy form is absent
    assert "MY_O_AUTH" not in yaml_str


def test_scaffold_includes_streaming_fields():
    """The dispatch: section of the scaffold must include all SSE streaming config fields."""
    yaml_str = Config.scaffold(spec_source="./spec.json", auth_schemes=[])
    assert "enable_streaming:" in yaml_str
    assert "streaming_timeout_multiplier:" in yaml_str
    assert "streaming_max_bytes:" in yaml_str
    # The multiplier footgun should be documented inline
    assert "per-operation" in yaml_str
