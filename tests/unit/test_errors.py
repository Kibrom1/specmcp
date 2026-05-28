"""Unit tests for the error taxonomy."""

from specmcp.errors import (
    ArgumentValidationError,
    AuthError,
    CLI_EXIT_CODES,
    ConfigEnvVarError,
    ConfigError,
    ConfigVersionError,
    DispatchError,
    InternalError,
    MCP_ERROR_CONTENT,
    NormalizeError,
    PipelineError,
    ResponseTooLargeError,
    SimplifyError,
    SourceLocation,
    SpecError,
    SpecmcpError,
    SpecResolutionError,
    SpecSyntaxError,
    SpecUnsupportedError,
    SpecValidationError,
    TransientError,
    UpstreamClientError,
    UpstreamServerError,
    exit_code_for,
    is_transient,
    mcp_error_content,
)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def test_base_error_minimal():
    exc = SpecmcpError("something went wrong")
    assert exc.message == "something went wrong"
    assert exc.detail is None
    assert exc.location is None
    assert exc.request_id is None
    assert exc.context == {}
    assert exc.code == "specmcp.error"


def test_base_error_full():
    loc = SourceLocation(file="spec.yaml", line=42)
    exc = SpecmcpError(
        "bad thing",
        detail="longer explanation",
        location=loc,
        request_id="req-123",
        context={"foo": "bar"},
    )
    assert str(exc) == "[spec.yaml:42] bad thing — longer explanation"
    assert exc.to_dict()["location"] == {"file": "spec.yaml", "line": 42}
    assert exc.to_dict()["context"] == {"foo": "bar"}


def test_source_location_str():
    loc = SourceLocation(file="petstore.yaml", line=8)
    assert str(loc) == "petstore.yaml:8"


def test_config_error():
    exc = ConfigError("bad config")
    assert exc.code == "config.error"
    assert isinstance(exc, SpecmcpError)


def test_config_env_var_error():
    exc = ConfigEnvVarError("env var STRIPE_API_KEY is not set")
    assert exc.code == "config.missing_env_var"
    # Auth values must NOT appear in message
    assert "STRIPE_API_KEY" in exc.message
    # The variable name is OK; what matters is the value never appears


def test_spec_errors():
    for cls, code in [
        (SpecSyntaxError, "spec.syntax_error"),
        (SpecValidationError, "spec.validation_error"),
        (SpecResolutionError, "spec.resolution_failed"),
        (SpecUnsupportedError, "spec.unsupported"),
    ]:
        exc = cls("msg")
        assert exc.code == code
        assert isinstance(exc, SpecError)


def test_pipeline_errors():
    for cls, code in [
        (NormalizeError, "pipeline.normalize_error"),
        (SimplifyError, "pipeline.simplify_error"),
    ]:
        exc = cls("msg")
        assert exc.code == code
        assert isinstance(exc, PipelineError)


def test_upstream_client_error():
    exc = UpstreamClientError("Not found", status_code=404, body="Pet not found")
    assert exc.code == "upstream.client_error"
    assert exc.status_code == 404
    assert exc.body == "Pet not found"
    assert exc.context["status_code"] == 404


def test_upstream_server_error():
    exc = UpstreamServerError("Internal error", status_code=500)
    assert exc.status_code == 500


def test_transient_error():
    exc = TransientError("connection reset")
    assert exc.transient is True
    assert is_transient(exc) is True


def test_is_transient_false_for_others():
    assert is_transient(ConfigError("x")) is False
    assert is_transient(UpstreamClientError("x", status_code=404)) is False


def test_argument_validation_error():
    exc = ArgumentValidationError("petId must be integer", schema_path="/petId")
    assert exc.schema_path == "/petId"
    assert exc.context["schema_path"] == "/petId"


def test_response_too_large():
    exc = ResponseTooLargeError("too big", response_bytes=2_000_000, limit_bytes=1_048_576)
    assert exc.response_bytes == 2_000_000
    assert exc.limit_bytes == 1_048_576
    assert exc.context["response_bytes"] == 2_000_000


# ---------------------------------------------------------------------------
# Exit code mapping — exhaustive
# ---------------------------------------------------------------------------


def test_exit_codes_exhaustive():
    """Every entry in CLI_EXIT_CODES must be an error class."""
    for cls, code in CLI_EXIT_CODES.items():
        assert issubclass(cls, SpecmcpError), f"{cls} must be a SpecmcpError subclass"
        assert isinstance(code, int)


def test_exit_code_for_config_error():
    assert exit_code_for(ConfigError("x")) == 64


def test_exit_code_for_spec_syntax():
    assert exit_code_for(SpecSyntaxError("x")) == 65


def test_exit_code_for_spec_unsupported():
    assert exit_code_for(SpecUnsupportedError("x")) == 69


def test_exit_code_for_pipeline():
    assert exit_code_for(PipelineError("x")) == 70
    assert exit_code_for(NormalizeError("x")) == 70
    assert exit_code_for(SimplifyError("x")) == 70


def test_exit_code_for_internal():
    assert exit_code_for(InternalError("x")) == 70


def test_exit_code_for_unknown_runtime():
    # Runtime errors (AuthError etc.) are not in CLI_EXIT_CODES —
    # they're only returned via MCP, not via CLI exit.
    assert exit_code_for(AuthError("x")) == 1


# ---------------------------------------------------------------------------
# MCP error content
# ---------------------------------------------------------------------------


def test_mcp_content_argument_validation():
    exc = ArgumentValidationError("petId must be integer")
    content = mcp_error_content(exc)
    assert "petId must be integer" in content


def test_mcp_content_upstream_client():
    exc = UpstreamClientError("Not found", status_code=404)
    content = mcp_error_content(exc)
    assert "404" in content


def test_mcp_content_transient_includes_request_id():
    exc = TransientError("timeout", request_id="req-abc")
    content = mcp_error_content(exc)
    assert "req-abc" in content


def test_mcp_content_auth_no_detail():
    """Auth errors must not reveal credential details in MCP content."""
    exc = AuthError("Bearer token rejected", request_id="req-xyz")
    content = mcp_error_content(exc)
    # Should mention "Authentication failed", not the raw message
    assert "Authentication failed" in content
    # Should not mention "Bearer token rejected" (sensitive)
    assert "Bearer token rejected" not in content


def test_mcp_content_dispatch_says_internal():
    exc = DispatchError("ArgumentMap produced invalid path")
    content = mcp_error_content(exc)
    assert "Internal error" in content
    # The raw implementation detail must not leak
    assert "ArgumentMap" not in content


def test_mcp_content_auth_required_with_login_url():
    """AuthRequiredError with a login URL must embed the URL in the message."""
    from specmcp.errors import AuthRequiredError
    exc = AuthRequiredError(
        "Need login",
        session_id="sess-123",
        login_url="http://localhost:8765/auth/login?nonce=abc",
    )
    content = mcp_error_content(exc)
    assert "http://localhost:8765/auth/login?nonce=abc" in content
    assert "None" not in content


def test_mcp_content_auth_required_without_login_url_does_not_say_none():
    """AuthRequiredError with login_url=None must NOT render the literal 'None'.

    When nonce issuance fails, login_url is None. The generic template
    would produce 'visit the following URL:\nNone\n' which is confusing.
    The fallback message must be coherent and not mention 'None'.
    """
    from specmcp.errors import AuthRequiredError
    exc = AuthRequiredError(
        "Auth required but nonce failed",
        session_id="sess-456",
        login_url=None,
    )
    content = mcp_error_content(exc)
    assert "None" not in content
    # Should still convey that auth is needed and give a next step
    assert "authentication" in content.lower() or "login" in content.lower()


def test_to_dict_roundtrip():
    exc = SpecSyntaxError(
        "bad YAML",
        location=SourceLocation("spec.yaml", 10),
        detail="unexpected token at line 10",
    )
    d = exc.to_dict()
    assert d["code"] == "spec.syntax_error"
    assert d["message"] == "bad YAML"
    assert d["detail"] == "unexpected token at line 10"
    assert d["location"] == {"file": "spec.yaml", "line": 10}
