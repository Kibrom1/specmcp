"""
specmcp error taxonomy.

All errors inherit from SpecmcpError. The hierarchy is fixed for v1;
adding new error kinds requires a design-doc update.

Exit codes follow sysexits.h conventions.
MCP error response shapes are defined by MCP_ERROR_MAP.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Source location
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLocation:
    """File + line reference for spec errors."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


# ---------------------------------------------------------------------------
# Base error
# ---------------------------------------------------------------------------


class SpecmcpError(Exception):
    """Base class for all specmcp errors."""

    #: Stable dot-separated identifier, e.g. "spec.resolution_failed"
    code: str = "specmcp.error"

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        location: SourceLocation | None = None,
        request_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.location = location
        self.request_id = request_id
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        prefix = f"[{self.location}] " if self.location else ""
        base = f"{prefix}{self.message}"
        if self.detail:
            return f"{base} — {self.detail}"
        return base

    def to_dict(self) -> dict[str, Any]:
        """Serialise to JSON-safe dict (for --json output)."""
        d: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.detail:
            d["detail"] = self.detail
        if self.location:
            d["location"] = {"file": self.location.file, "line": self.location.line}
        if self.request_id:
            d["request_id"] = self.request_id
        if self.context:
            d["context"] = self.context
        return d


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------


class ConfigError(SpecmcpError):
    """Bad config, missing env vars, unresolved refs in config."""

    code = "config.error"


class ConfigVersionError(ConfigError):
    """Config file version is not supported."""

    code = "config.unsupported_version"


class ConfigEnvVarError(ConfigError):
    """A required environment variable is missing."""

    code = "config.missing_env_var"


# ---------------------------------------------------------------------------
# Spec errors
# ---------------------------------------------------------------------------


class SpecError(SpecmcpError):
    """Any problem with the input OpenAPI spec."""

    code = "spec.error"


class SpecSyntaxError(SpecError):
    """YAML / JSON parse failure."""

    code = "spec.syntax_error"


class SpecValidationError(SpecError):
    """Spec fails the OpenAPI meta-schema."""

    code = "spec.validation_error"


class SpecResolutionError(SpecError):
    """A $ref could not be resolved."""

    code = "spec.resolution_failed"


class SpecUnsupportedError(SpecError):
    """Valid spec, but a feature is not yet supported (e.g. Swagger 2.0)."""

    code = "spec.unsupported"


# ---------------------------------------------------------------------------
# Pipeline errors
# ---------------------------------------------------------------------------


class PipelineError(SpecmcpError):
    """Internal pipeline failure — indicates a bug in specmcp."""

    code = "pipeline.error"


class NormalizeError(PipelineError):
    """Failure in the Normalize stage."""

    code = "pipeline.normalize_error"


class SimplifyError(PipelineError):
    """Failure in the Simplify stage."""

    code = "pipeline.simplify_error"


# ---------------------------------------------------------------------------
# Runtime errors  (raised during tools/call)
# ---------------------------------------------------------------------------


class RuntimeError(SpecmcpError):  # noqa: A001  (shadows built-in intentionally)
    """Base for errors raised during MCP tool invocation."""

    code = "runtime.error"


class ArgumentValidationError(RuntimeError):
    """LLM-supplied args fail the tool's input schema."""

    code = "argument.validation_failed"

    def __init__(
        self,
        message: str,
        *,
        schema_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.schema_path = schema_path
        if schema_path:
            self.context["schema_path"] = schema_path


class DispatchError(RuntimeError):
    """ArgumentMap → HTTP construction failed. Indicates a bug in specmcp."""

    code = "runtime.dispatch_error"


class AuthError(RuntimeError):
    """Auth scheme could not apply credentials or the upstream refused them."""

    code = "runtime.auth_error"


class AuthConfigError(AuthError):
    """A required auth scheme is not configured or has an unsupported type.

    Raised at dispatch time (not startup) so that unconfigured schemes only
    block the operations that actually need them. The message must never
    contain credential values.
    """

    code = "runtime.auth_config_error"


class AuthRequiredError(AuthError):
    """No OAuth token exists for this session; user must authenticate via login URL.

    Raised by the auth layer when an Authorization Code flow operation is called
    but the session has no valid token. The dispatcher catches this and returns
    an actionable message to the LLM containing the single-use login URL.

    The nonce (not the session_id) is included in the login URL. The session_id
    is never surfaced to the LLM.
    """

    code = "runtime.auth_required"

    def __init__(
        self,
        message: str = "Authentication required",
        *,
        session_id: str | None = None,
        login_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.session_id = session_id
        self.login_url = login_url
        if login_url:
            self.context["login_url"] = login_url


class TokenRefreshError(AuthError):
    """OAuth token endpoint request failed or returned an unexpected response.

    The message must NEVER contain the client_secret or the access_token —
    only the token_url and the HTTP status code are safe to include.
    """

    code = "runtime.token_refresh_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.status_code = status_code
        if status_code is not None:
            self.context["status_code"] = status_code


class UpstreamClientError(RuntimeError):
    """Upstream returned a 4xx response."""

    code = "upstream.client_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        body: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.status_code = status_code
        self.body = body
        self.context["status_code"] = status_code


class UpstreamServerError(RuntimeError):
    """Upstream returned a 5xx response."""

    code = "upstream.server_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.status_code = status_code
        self.context["status_code"] = status_code


class TransientError(RuntimeError):
    """Network error: DNS, connect, TLS, or read timeout.

    The ``transient`` flag in the MCP _meta signals to orchestrators that
    a retry at their layer may succeed.
    """

    code = "runtime.transient_error"
    transient: bool = True


class ResponseTooLargeError(RuntimeError):
    """Response body exceeded the configured size cap."""

    code = "runtime.response_too_large"

    def __init__(
        self,
        message: str,
        *,
        response_bytes: int,
        limit_bytes: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.response_bytes = response_bytes
        self.limit_bytes = limit_bytes
        self.context.update({"response_bytes": response_bytes, "limit_bytes": limit_bytes})


class InternalError(SpecmcpError):
    """Genuine specmcp bug. Never shown to users without context."""

    code = "internal.error"


# ---------------------------------------------------------------------------
# CLI exit code mapping   (sysexits.h conventions)
# ---------------------------------------------------------------------------

#: Maps error class → (exit_code, stream).
#: Stream is "stderr" always; kept explicit for clarity.
CLI_EXIT_CODES: dict[type[SpecmcpError], int] = {
    ConfigError: 64,          # EX_USAGE
    ConfigVersionError: 64,
    ConfigEnvVarError: 64,
    SpecSyntaxError: 65,      # EX_DATAERR
    SpecValidationError: 65,
    SpecResolutionError: 65,
    SpecUnsupportedError: 69, # EX_UNAVAILABLE
    PipelineError: 70,        # EX_SOFTWARE
    NormalizeError: 70,
    SimplifyError: 70,
    InternalError: 70,
}


def exit_code_for(exc: SpecmcpError) -> int:
    """Return the sysexits.h exit code for an exception instance."""
    for cls in type(exc).__mro__:
        if cls in CLI_EXIT_CODES:
            return CLI_EXIT_CODES[cls]
    return 1  # generic failure


# ---------------------------------------------------------------------------
# MCP error response mapping
# ---------------------------------------------------------------------------

#: What the LLM sees in the MCP content blocks for each runtime error.
#: Keys are error classes; values are content-block templates.
#: ``{exc}`` is substituted with str(exc); ``{request_id}`` with exc.request_id.
MCP_ERROR_CONTENT: dict[type[SpecmcpError], str] = {
    ArgumentValidationError: "Invalid argument: {exc}",
    AuthRequiredError:       "Authentication required. Please ask the user to visit the following URL to log in:\n{exc.login_url}\n\nThe link expires in 5 minutes. After logging in, retry your request.",
    UpstreamClientError:     "Upstream returned HTTP {exc.status_code}: {exc.message}",
    UpstreamServerError:     "Upstream service error (request_id: {request_id})",
    TransientError:          "Network error (request_id: {request_id}). This may be transient.",
    TokenRefreshError:       "OAuth token refresh failed (request_id: {request_id})",
    AuthError:               "Authentication failed (request_id: {request_id})",
    ResponseTooLargeError:   "Response too large ({exc.response_bytes} bytes, limit {exc.limit_bytes} bytes)",
    DispatchError:           "Internal error (request_id: {request_id})",
    InternalError:           "Internal error (request_id: {request_id})",
}


def mcp_error_content(exc: SpecmcpError) -> str:
    """Format the human-readable MCP error content block text."""
    request_id = exc.request_id or "unknown"
    for cls in type(exc).__mro__:
        if cls in MCP_ERROR_CONTENT:
            template = MCP_ERROR_CONTENT[cls]
            try:
                return template.format(exc=exc, request_id=request_id)
            except (AttributeError, KeyError):
                return str(exc)
    return str(exc)


def is_transient(exc: SpecmcpError) -> bool:
    """Return True if the error should carry ``transient: true`` in MCP _meta."""
    return isinstance(exc, TransientError)
