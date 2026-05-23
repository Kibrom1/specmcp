"""
specmcp configuration schema.

The config file is ``mcp.config.yaml`` by default.
All values are validated with Pydantic v2 at load time.

The ``value_from: env(VAR_NAME)`` DSL is resolved at load time.
Missing env vars raise ConfigEnvVarError immediately — the server
never starts in a partially-authenticated state.

Auth values are stored as SensitiveStr so they never appear in
repr(), str(), or exception messages.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from specmcp.errors import ConfigEnvVarError, ConfigError, ConfigVersionError

# ---------------------------------------------------------------------------
# SensitiveStr — auth values are stored as this type.
# Its repr/str return "<redacted>" so accidental logging is safe.
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"^env\(([A-Z_][A-Z0-9_]*)\)$")
SUPPORTED_CONFIG_VERSIONS = {"1", "2"}

# ---------------------------------------------------------------------------
# OAuth field-level validation constants
# ---------------------------------------------------------------------------

# Fields that specmcp controls; operators must not inject them via extra_params.
_RESERVED_OAUTH_PARAMS = frozenset({
    "grant_type",
    "code",
    "code_verifier",
    "redirect_uri",
    "client_id",
    "client_secret",
    "response_type",
})


def scheme_name_to_env_var(name: str) -> str:
    """Convert a securityScheme name to an UPPER_SNAKE_CASE env var name.

    Two-pass approach (Python's re doesn't support variable-width lookbehinds):

    Pass 1 — lowercase/digit → uppercase boundary:   myApiKey  → my_Api_Key
    Pass 2 — uppercase run → title word boundary:    HTMLParser → HTML_Parser
              (only fires when 2+ consecutive uppercase precede the title word,
               so a lone uppercase like the 'O' in 'myOAuth' is kept together)

    Examples:
        petstoreApiKey  → PETSTORE_API_KEY
        myBearerAuth    → MY_BEARER_AUTH
        myOAuth         → MY_OAUTH
        HTMLParser      → HTML_PARSER
        OAuth2Flow      → OAUTH2_FLOW
        stripe-api-key  → STRIPE_API_KEY
    """
    # Pass 1: lowercase/digit followed by uppercase
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    # Pass 2: run of 2+ uppercase letters followed by a Title word (e.g. HTMLParser)
    s = re.sub(r"([A-Z]{2,})([A-Z][a-z])", r"\1_\2", s)
    # Uppercase and collapse any non-alphanumeric separators to underscores
    return re.sub(r"[^A-Z0-9]+", "_", s.upper()).strip("_")


class SensitiveStr(str):
    """A str subclass whose repr and str always return '<redacted>'."""

    def __repr__(self) -> str:  # noqa: D105
        return "<redacted>"

    def __str__(self) -> str:  # noqa: D105
        return "<redacted>"

    def reveal(self) -> str:
        """Return the actual value. Only call this when actually needed."""
        return str.__str__(self)


def _resolve_value_from(raw: str, scheme_name: str) -> SensitiveStr:
    """Parse ``env(VAR_NAME)`` and return the env var value as SensitiveStr.

    Raises ConfigEnvVarError (with the variable *name*, never the value)
    if the variable is not set.
    """
    m = _ENV_VAR_RE.match(raw.strip())
    if not m:
        raise ConfigError(
            f"Auth scheme '{scheme_name}': value_from must be in the form "
            f"env(VAR_NAME), got: {raw!r}"
        )
    var_name = m.group(1)
    value = os.environ.get(var_name)
    if value is None:
        raise ConfigEnvVarError(
            f"Auth scheme '{scheme_name}' requires environment variable "
            f"{var_name!r} but it is not set. "
            f"Add it to your shell environment or .env file."
        )
    return SensitiveStr(value)


# ---------------------------------------------------------------------------
# Auth config
# ---------------------------------------------------------------------------


class ApiKeyAuthConfig(BaseModel):
    type: Literal["apiKey"]
    in_: Literal["header", "query", "cookie"] = Field(alias="in")
    name: str
    value_from: str  # raw string like "env(MY_API_KEY)"

    model_config = {"populate_by_name": True}

    def resolve(self, scheme_name: str) -> SensitiveStr:
        return _resolve_value_from(self.value_from, scheme_name)


class BearerAuthConfig(BaseModel):
    type: Literal["bearer"]
    value_from: str

    def resolve(self, scheme_name: str) -> SensitiveStr:
        return _resolve_value_from(self.value_from, scheme_name)


def _validate_token_url(url: str, field_name: str = "token_url") -> str:
    """Enforce https:// on token/auth URLs.

    Allows http:// only for localhost/127.0.0.1/::1 (local dev/testing).
    Raises ValueError on violation — Pydantic converts this to a ConfigError
    via the model_validator in Config.parse_auth_schemes.
    """
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return url
        raise ValueError(
            f"{field_name} must use https:// in production environments. "
            f"Got: {url!r}. HTTP is only allowed for localhost."
        )
    raise ValueError(
        f"{field_name} must start with https:// (or http://localhost for dev). Got: {url!r}"
    )


def _validate_extra_params(v: dict[str, str]) -> dict[str, str]:
    """Reject reserved OAuth parameter names in extra_params."""
    reserved = _RESERVED_OAUTH_PARAMS & v.keys()
    if reserved:
        raise ValueError(
            f"extra_params must not contain reserved OAuth fields: {sorted(reserved)}. "
            f"These are controlled by specmcp and cannot be overridden."
        )
    return v


class OAuth2ClientCredentialsConfig(BaseModel):
    """OAuth 2.0 client_credentials flow.

    Credentials are exchanged for a short-lived access token at the token_url.
    The token is cached in memory and refreshed automatically before expiry.

    Example mcp.config.yaml::

        auth:
          myOAuth:
            type: oauth2_client_credentials
            token_url: https://auth.example.com/oauth/token
            client_id_from: env(MY_CLIENT_ID)
            client_secret_from: env(MY_CLIENT_SECRET)
            scopes:
              - read
              - write
    """

    type: Literal["oauth2_client_credentials"]
    token_url: str
    client_id_from: str    # env(VAR_NAME) DSL, same as value_from elsewhere
    client_secret_from: str
    scopes: list[str] = Field(default_factory=list)
    # Extra form fields for non-standard token endpoints (e.g. audience, resource)
    extra_params: dict[str, str] = Field(default_factory=dict)

    @field_validator("token_url")
    @classmethod
    def check_token_url_scheme(cls, v: str) -> str:
        return _validate_token_url(v, "token_url")

    @field_validator("extra_params")
    @classmethod
    def check_extra_params(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_extra_params(v)


class OAuth2AuthorizationCodeConfig(BaseModel):
    """OAuth 2.0 Authorization Code + PKCE flow (version "2" configs only).

    Each user authenticates via their own browser. specmcp issues a single-use
    login URL, the user logs in, and specmcp stores per-session tokens in a
    TokenStore.

    Requires config version "2".
    """

    type: Literal["oauth2_authorization_code"]
    authorization_url: str      # must be https:// (or localhost)
    token_url: str              # must be https:// (or localhost)
    client_id_from: str
    client_secret_from: str | None = None   # optional for public clients
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str
    token_store: Literal["memory", "sqlite"] = "memory"
    token_store_path: str | None = None
    token_store_key_from: str | None = None  # required when token_store=sqlite
    extra_params: dict[str, str] = Field(default_factory=dict)

    @field_validator("authorization_url")
    @classmethod
    def check_authorization_url_scheme(cls, v: str) -> str:
        return _validate_token_url(v, "authorization_url")

    @field_validator("token_url")
    @classmethod
    def check_token_url_scheme(cls, v: str) -> str:
        return _validate_token_url(v, "token_url")

    @field_validator("extra_params")
    @classmethod
    def check_extra_params(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_extra_params(v)

    @model_validator(mode="after")
    def check_sqlite_requires_key(self) -> "OAuth2AuthorizationCodeConfig":
        if self.token_store == "sqlite" and not self.token_store_key_from:
            raise ValueError(
                "token_store_key_from is required when token_store is 'sqlite'. "
                "Set it to env(TOKEN_STORE_KEY) and export the variable."
            )
        return self


AuthSchemeConfig = (
    ApiKeyAuthConfig
    | BearerAuthConfig
    | OAuth2ClientCredentialsConfig
    | OAuth2AuthorizationCodeConfig
)


# ---------------------------------------------------------------------------
# Per-operation retry config
# ---------------------------------------------------------------------------


class RetryConfig(BaseModel):
    attempts: int = Field(default=2, ge=1, le=5)
    on_status: list[int] = Field(default_factory=lambda: [503, 504])


# ---------------------------------------------------------------------------
# Per-operation overrides
# ---------------------------------------------------------------------------


class OperationOverride(BaseModel):
    rename: str | None = None
    description: str | None = None
    hide: bool = False
    server_url: str | None = None
    timeout_seconds: float | None = None
    retry: RetryConfig | None = None
    pin_request_body_variant: str | None = None
    additional_properties_strict: bool = False


# ---------------------------------------------------------------------------
# Simplify config
# ---------------------------------------------------------------------------


class SimplifyConfig(BaseModel):
    inline_shallow_refs: bool = True
    drop_spec_metadata: bool = True
    collapse_unions: bool = True
    flatten_single_property_wrappers: bool = True
    truncate_description_chars: int = Field(default=500, ge=50)


# ---------------------------------------------------------------------------
# Dispatch config
# ---------------------------------------------------------------------------


class DispatchConfig(BaseModel):
    default_timeout_seconds: float = 30.0
    per_host_concurrency: int = Field(default=10, ge=1, le=200)
    global_concurrency: int = Field(default=32, ge=1, le=500)
    response_size_limit_bytes: int = Field(default=1_048_576)    # 1 MiB
    text_truncate_bytes: int = Field(default=262_144)            # 256 KiB
    tls_verify: bool = True
    # SSE / streaming settings
    enable_streaming: bool = True
    # Multiplier applied to the resolved timeout for SSE operations.
    # A 30s default timeout becomes 150s for streaming. Per-operation
    # timeout_seconds overrides are also multiplied.
    streaming_timeout_multiplier: float = Field(default=5.0, ge=1.0)
    # Maximum bytes to buffer from an SSE stream before truncating.
    # Prevents OOM on indefinite or misbehaving streams.
    streaming_max_bytes: int = Field(default=4 * 1024 * 1024)   # 4 MiB


# ---------------------------------------------------------------------------
# Transport config
# ---------------------------------------------------------------------------


class HttpTransportConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class TransportConfig(BaseModel):
    default: Literal["stdio", "http"] = "stdio"
    http: HttpTransportConfig = Field(default_factory=HttpTransportConfig)


# ---------------------------------------------------------------------------
# Spec source config
# ---------------------------------------------------------------------------


class SpecConfig(BaseModel):
    source: str  # local path or URL; required
    cache: bool = True


# ---------------------------------------------------------------------------
# Server-level filtering
# ---------------------------------------------------------------------------


class ServerConfig(BaseModel):
    base_url_override: str | None = None
    include_deprecated: bool = True
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    include_operations: list[str] = Field(default_factory=list)
    exclude_operations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging config
# ---------------------------------------------------------------------------


class LoggingConfig(BaseModel):
    level: Literal["debug", "info", "warn", "error"] = "info"
    format: Literal["json", "text"] = "json"


# ---------------------------------------------------------------------------
# Telemetry config
# ---------------------------------------------------------------------------


class TelemetryConfig(BaseModel):
    enabled: bool = False


# ---------------------------------------------------------------------------
# Management endpoint config (version "2" only)
# ---------------------------------------------------------------------------


class ManagementConfig(BaseModel):
    """Controls access to the management HTTP endpoint (DELETE /auth/session/<id>).

    bind: loopback — only reachable from 127.0.0.1/::1 (default, safe for single-host)
    bind: all      — reachable from any interface; management_token_from is required
    """

    bind: Literal["loopback", "all"] = "loopback"
    management_token_from: str | None = None  # env(VAR_NAME); required if bind=all

    @model_validator(mode="after")
    def check_all_requires_token(self) -> "ManagementConfig":
        if self.bind == "all" and not self.management_token_from:
            raise ValueError(
                "management_token_from is required when bind is 'all'. "
                "Set it to env(SPECMCP_MANAGEMENT_TOKEN)."
            )
        return self


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Top-level specmcp configuration model."""

    version: str
    spec: SpecConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: dict[str, Any] = Field(default_factory=dict)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    simplify: SimplifyConfig = Field(default_factory=SimplifyConfig)
    operations: dict[str, OperationOverride] = Field(default_factory=dict)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    management: ManagementConfig = Field(default_factory=ManagementConfig)

    # Parsed auth schemes (populated after load)
    _auth_schemes: dict[str, AuthSchemeConfig] = {}

    @field_validator("version")
    @classmethod
    def check_version(cls, v: str) -> str:
        if v not in SUPPORTED_CONFIG_VERSIONS:
            raise ValueError(
                f"Config version {v!r} is not supported. "
                f"Supported versions: {sorted(SUPPORTED_CONFIG_VERSIONS)}"
            )
        return v

    @model_validator(mode="after")
    def parse_auth_schemes(self) -> "Config":
        """Parse and validate auth scheme entries."""
        parsed: dict[str, AuthSchemeConfig] = {}
        for name, raw in self.auth.items():
            if not isinstance(raw, dict):
                raise ConfigError(f"Auth scheme '{name}' must be a mapping, got {type(raw)}")
            scheme_type = raw.get("type")
            try:
                if scheme_type == "apiKey":
                    parsed[name] = ApiKeyAuthConfig.model_validate(raw)
                elif scheme_type == "bearer":
                    parsed[name] = BearerAuthConfig.model_validate(raw)
                elif scheme_type == "oauth2_client_credentials":
                    parsed[name] = OAuth2ClientCredentialsConfig.model_validate(raw)
                elif scheme_type == "oauth2_authorization_code":
                    if self.version != "2":
                        raise ConfigError(
                            f"Auth scheme '{name}': type 'oauth2_authorization_code' requires "
                            f"config version \"2\". Your config uses version {self.version!r}. "
                            f"Update the 'version' key to \"2\" to enable this feature."
                        )
                    parsed[name] = OAuth2AuthorizationCodeConfig.model_validate(raw)
                else:
                    raise ConfigError(
                        f"Auth scheme '{name}': unsupported type {scheme_type!r}. "
                        f"Supported: apiKey, bearer, oauth2_client_credentials, "
                        f"oauth2_authorization_code (requires version \"2\")."
                    )
            except Exception as exc:
                if isinstance(exc, ConfigError):
                    raise
                raise ConfigError(
                    f"Auth scheme '{name}' is invalid: {exc}"
                ) from exc
        self._auth_schemes = parsed
        return self

    def get_auth_scheme(self, name: str) -> AuthSchemeConfig | None:
        return self._auth_schemes.get(name)

    def resolve_auth_values(self) -> dict[str, SensitiveStr]:
        """Resolve static auth env vars. Raises ConfigEnvVarError if any are missing.

        OAuth2ClientCredentialsConfig schemes are intentionally excluded: their
        client_id and client_secret are resolved lazily by AuthInjector.build()
        so they can be stored as SensitiveStr on ResolvedScheme.

        Call this at startup (validate / serve), not at config load time,
        so that ``init`` and ``inspect`` can work without real credentials.
        """
        resolved: dict[str, SensitiveStr] = {}
        for name, scheme in self._auth_schemes.items():
            if isinstance(scheme, (OAuth2ClientCredentialsConfig, OAuth2AuthorizationCodeConfig)):
                continue  # credentials resolved by AuthInjector.build()
            resolved[name] = scheme.resolve(name)  # type: ignore[union-attr]
        return resolved

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        """Load and validate a config file from *path*.

        Raises:
            ConfigError: if the file cannot be read or parsed.
            ConfigVersionError: if the version is not supported.
        """
        from ruamel.yaml import YAML  # lazy import — ruamel is only needed here

        yaml = YAML(typ="safe")
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            raw = yaml.load(path)
        except Exception as exc:
            raise ConfigError(
                f"Failed to parse config file {path}: {exc}",
                detail=str(exc),
            ) from exc

        if not isinstance(raw, dict):
            raise ConfigError(f"Config file must be a YAML mapping, got {type(raw).__name__}")

        # Check version before full validation for a cleaner error message.
        version = raw.get("version")
        if version not in SUPPORTED_CONFIG_VERSIONS:
            raise ConfigVersionError(
                f"Config version {version!r} is not supported. "
                f"Supported versions: {sorted(SUPPORTED_CONFIG_VERSIONS)}. "
                f"See the migration guide if you're upgrading."
            )

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(
                f"Config file {path} is invalid: {exc}",
                detail=str(exc),
            ) from exc

    @classmethod
    def scaffold(cls, spec_source: str, auth_schemes: list[dict[str, Any]]) -> str:
        """Return a YAML string scaffold for a new config file.

        Used by ``specmcp init``.
        """
        lines = [
            "# specmcp configuration — generated by 'specmcp init'",
            "# See https://github.com/specmcp/specmcp for full reference.",
            "",
            'version: "1"',
            "",
            "spec:",
            f'  source: "{spec_source}"',
            "  cache: true",
            "",
            "server:",
            "  include_deprecated: true",
            "  include_tags: []",
            "  exclude_tags: []",
            "  include_operations: []",
            "  exclude_operations: []",
            "",
            "dispatch:",
            "  default_timeout_seconds: 30",
            "  per_host_concurrency: 10",
            "  global_concurrency: 32",
            "  response_size_limit_bytes: 1048576  # 1 MiB",
            "  text_truncate_bytes: 262144         # 256 KiB",
            "  tls_verify: true",
            "  # SSE / streaming — set enable_streaming: false to force buffered mode (useful for debugging)",
            "  enable_streaming: true",
            "  # Multiplier applied to the resolved timeout for text/event-stream operations.",
            "  # A default_timeout_seconds of 30 becomes 150s for streaming calls.",
            "  # IMPORTANT: per-operation timeout_seconds overrides are also multiplied.",
            "  # To cap a streaming operation at exactly N seconds, set timeout_seconds: N",
            "  # here AND set streaming_timeout_multiplier: 1.0.",
            "  streaming_timeout_multiplier: 5.0",
            "  streaming_max_bytes: 4194304         # 4 MiB — truncates runaway streams",
            "",
            "simplify:",
            "  inline_shallow_refs: true",
            "  drop_spec_metadata: true",
            "  collapse_unions: true",
            "  flatten_single_property_wrappers: true",
            "  truncate_description_chars: 500",
            "",
            "transport:",
            "  default: stdio",
            "  http:",
            "    host: 127.0.0.1",
            "    port: 8765",
            "",
            "telemetry:",
            "  enabled: false",
            "",
            "logging:",
            "  level: info",
            "  format: json",
            "",
        ]

        if auth_schemes:
            lines.append("auth:")
            env_vars: list[str] = []
            for scheme in auth_schemes:
                name = scheme["name"]
                scheme_type = scheme.get("type", "apiKey")
                env_var = scheme_name_to_env_var(name)
                env_vars.append(env_var)

                if scheme_type == "apiKey":
                    in_ = scheme.get("in", "header")
                    header_name = scheme.get("header_name", "X-Api-Key")
                    lines += [
                        f"  {name}:",
                        '    type: apiKey',
                        f'    in: {in_}',
                        f'    name: {header_name}',
                        f'    value_from: env({env_var})',
                    ]
                elif scheme_type == "http" and scheme.get("scheme") == "bearer":
                    lines += [
                        f"  {name}:",
                        '    type: bearer',
                        f'    value_from: env({env_var})',
                    ]
                elif scheme_type == "oauth2" or scheme.get("scheme") == "oauth2":
                    client_id_var = f"{env_var}_CLIENT_ID"
                    client_secret_var = f"{env_var}_CLIENT_SECRET"
                    token_url = scheme.get("token_url", "https://auth.example.com/oauth/token")
                    lines += [
                        f"  {name}:",
                        "    type: oauth2_client_credentials",
                        f"    token_url: {token_url}",
                        f"    client_id_from: env({client_id_var})",
                        f"    client_secret_from: env({client_secret_var})",
                        "    scopes: []",
                    ]
                else:
                    lines += [
                        f"  # {name}:",
                        f"  #   type: {scheme_type}  # Not yet supported",
                    ]
            lines.append("")
        else:
            lines += [
                "# auth:",
                "#   myScheme:",
                "#     type: apiKey",
                "#     in: header",
                "#     name: X-Api-Key",
                "#     value_from: env(MY_API_KEY)",
                "",
            ]

        lines += [
            "# operations:",
            "#   myOperationId:",
            "#     rename: my_tool",
            "#     description: 'Custom description for the LLM.'",
            "#     hide: false",
            "#     timeout_seconds: 60",
        ]

        return "\n".join(lines) + "\n"
