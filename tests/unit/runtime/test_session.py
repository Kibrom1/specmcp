"""Unit tests for specmcp.runtime.session.SessionContext."""

from __future__ import annotations

from specmcp.config import SensitiveStr
from specmcp.runtime.session import SessionContext


def test_session_context_default_fields():
    """SessionContext is created with session_id; optional fields default to None/{}."""
    sc = SessionContext(session_id="test-id-123")
    assert sc.session_id == "test-id-123"
    assert sc.client_token is None
    assert sc.metadata == {}


def test_session_context_with_client_token():
    """SessionContext stores a SensitiveStr client_token that redacts on str()."""
    token = SensitiveStr("secret-bearer-token")
    sc = SessionContext(session_id="sid", client_token=token)
    assert sc.client_token is not None
    # SensitiveStr must not leak value through str()
    assert "secret-bearer-token" not in str(sc.client_token)
    assert sc.client_token.reveal() == "secret-bearer-token"


def test_session_context_with_metadata():
    """SessionContext stores arbitrary metadata dict."""
    sc = SessionContext(session_id="sid", metadata={"user": "alice", "tier": "pro"})
    assert sc.metadata["user"] == "alice"
    assert sc.metadata["tier"] == "pro"


def test_session_context_mutable_client_token():
    """client_token can be updated after construction (lazy init from first tool call)."""
    sc = SessionContext(session_id="sid")
    assert sc.client_token is None
    sc.client_token = SensitiveStr("new-token")
    assert sc.client_token is not None
    assert sc.client_token.reveal() == "new-token"


def test_session_repr_does_not_leak_token():
    """repr() of SessionContext must not include the client_token value."""
    sc = SessionContext(
        session_id="sid",
        client_token=SensitiveStr("super-secret"),
    )
    assert "super-secret" not in repr(sc)
