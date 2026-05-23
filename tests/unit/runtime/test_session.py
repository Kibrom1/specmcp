"""Unit tests for specmcp.runtime.session.SessionContext."""

from __future__ import annotations

import pytest

from specmcp.config import SensitiveStr
from specmcp.runtime.session import SessionContext


def test_session_context_minimal():
    """SessionContext can be created with just a session_id."""
    sc = SessionContext(session_id="test-uuid-1234")
    assert sc.session_id == "test-uuid-1234"
    assert sc.client_token is None
    assert sc.metadata == {}


def test_session_context_metadata_defaults_empty():
    """metadata field defaults to a fresh empty dict (not shared across instances)."""
    sc1 = SessionContext(session_id="s1")
    sc2 = SessionContext(session_id="s2")
    sc1.metadata["key"] = "value"
    assert sc2.metadata == {}, "metadata should not be shared between instances"


def test_session_context_with_client_token():
    """client_token is stored as SensitiveStr."""
    token = SensitiveStr("super-secret-bearer-token")
    sc = SessionContext(session_id="s1", client_token=token)
    assert sc.client_token is token
    assert sc.client_token.reveal() == "super-secret-bearer-token"
    # SensitiveStr must not leak in repr or str
    assert "super-secret-bearer-token" not in str(sc.client_token)
    assert "super-secret-bearer-token" not in repr(sc.client_token)


def test_session_context_with_metadata():
    """metadata dict can be passed at construction time."""
    sc = SessionContext(
        session_id="s1",
        metadata={"client_name": "my-mcp-client", "version": "1.0"},
    )
    assert sc.metadata["client_name"] == "my-mcp-client"


def test_session_context_session_id_is_string():
    """session_id is a plain str, not a SensitiveStr (it is not a secret)."""
    sc = SessionContext(session_id="visible-id")
    assert isinstance(sc.session_id, str)
    assert str(sc.session_id) == "visible-id"
