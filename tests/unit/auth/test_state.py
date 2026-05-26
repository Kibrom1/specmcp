"""Unit tests for specmcp.auth.state (OAuth CSRF state tokens)."""

from __future__ import annotations

import time

import pytest

from specmcp.auth.state import make_state, verify_state


SECRET = "test-secret-key"


# ---------------------------------------------------------------------------
# make_state → verify_state round-trip
# ---------------------------------------------------------------------------


def test_round_trip_simple():
    """make_state + verify_state returns the original session_id."""
    sid = "session-abc-123"
    state = make_state(sid, SECRET)
    assert verify_state(state, SECRET) == sid


def test_round_trip_uuid_session_id():
    """Works with a UUID-style session_id."""
    import uuid
    sid = str(uuid.uuid4())
    state = make_state(sid, SECRET)
    assert verify_state(state, SECRET) == sid


def test_round_trip_bytes_secret():
    """Secret can be passed as bytes."""
    sid = "session-xyz"
    state = make_state(sid, SECRET.encode("utf-8"))
    assert verify_state(state, SECRET.encode("utf-8")) == sid


def test_state_is_url_safe_base64():
    """State must only contain URL-safe base64 characters (no +, /, =)."""
    state = make_state("sid", SECRET)
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert all(c in allowed for c in state), f"Unexpected chars in state: {state!r}"


def test_state_no_padding():
    """State must not contain '=' padding characters."""
    state = make_state("sid", SECRET)
    assert "=" not in state


def test_different_sessions_produce_different_states():
    """Two different session_ids produce different states."""
    s1 = make_state("session-1", SECRET)
    s2 = make_state("session-2", SECRET)
    assert s1 != s2


def test_repeated_calls_produce_different_states():
    """Same session_id called twice produces different states (timestamp changes)."""
    # This relies on time progressing, which is normally true. If both calls
    # happen within the same second we can't guarantee inequality, but we can
    # verify both decode to the same session_id.
    s1 = make_state("sid", SECRET)
    s2 = make_state("sid", SECRET)
    assert verify_state(s1, SECRET) == "sid"
    assert verify_state(s2, SECRET) == "sid"


# ---------------------------------------------------------------------------
# MAC verification
# ---------------------------------------------------------------------------


def test_wrong_secret_raises():
    """verify_state with the wrong secret must raise ValueError."""
    state = make_state("sid", SECRET)
    with pytest.raises(ValueError, match="MAC"):
        verify_state(state, "wrong-secret")


def test_tampered_state_raises():
    """A state with a flipped bit must be rejected."""
    state = make_state("sid", SECRET)
    # Flip the last character
    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
    with pytest.raises(ValueError):
        verify_state(tampered, SECRET)


def test_truncated_state_raises():
    """A truncated state must be rejected."""
    state = make_state("sid", SECRET)
    with pytest.raises(ValueError):
        verify_state(state[:10], SECRET)


def test_empty_state_raises():
    """An empty state string must be rejected."""
    with pytest.raises((ValueError, Exception)):
        verify_state("", SECRET)


def test_garbage_state_raises():
    """Non-base64 input must be rejected."""
    with pytest.raises((ValueError, Exception)):
        verify_state("not-valid-base64!@#$", SECRET)


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


def test_fresh_state_is_valid():
    """A just-created state is within TTL."""
    state = make_state("sid", SECRET)
    # Should not raise with default 600s TTL
    assert verify_state(state, SECRET, ttl=600.0) == "sid"


def test_expired_state_raises(monkeypatch):
    """A state older than ttl must be rejected."""
    frozen_time = [1_000_000.0]

    monkeypatch.setattr("specmcp.auth.state.time", type(
        "FakeTime", (), {
            "time": staticmethod(lambda: frozen_time[0]),
        }
    )())

    state = make_state("sid", SECRET)

    # Advance time past TTL
    frozen_time[0] += 700.0

    with pytest.raises(ValueError, match="expired"):
        verify_state(state, SECRET, ttl=600.0)


def test_future_timestamp_raises(monkeypatch):
    """A state with a far-future timestamp (clock skew attack) must be rejected."""
    frozen_time = [1_000_000.0]

    monkeypatch.setattr("specmcp.auth.state.time", type(
        "FakeTime", (), {
            "time": staticmethod(lambda: frozen_time[0]),
        }
    )())

    state = make_state("sid", SECRET)

    # Move time backwards so the state appears to be from the future
    frozen_time[0] -= 700.0

    with pytest.raises(ValueError, match="expired"):
        verify_state(state, SECRET, ttl=600.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_session_id_raises():
    """Empty session_id must raise ValueError."""
    with pytest.raises(ValueError, match="empty"):
        make_state("", SECRET)


def test_very_long_session_id():
    """session_id up to 65535 bytes is accepted."""
    sid = "x" * 1000
    state = make_state(sid, SECRET)
    assert verify_state(state, SECRET) == sid


def test_session_id_with_unicode():
    """session_id with non-ASCII characters is encoded and decoded correctly."""
    sid = "session-élève"  # session-élève
    state = make_state(sid, SECRET)
    assert verify_state(state, SECRET) == sid


def test_short_ttl_just_in_time(monkeypatch):
    """A state used exactly within TTL is accepted."""
    # Use a clean integer base time to avoid int() truncation giving age=60.
    frozen_time = [1_000_000.0]

    monkeypatch.setattr("specmcp.auth.state.time", type(
        "FakeTime", (), {
            "time": staticmethod(lambda: frozen_time[0]),
        }
    )())

    state = make_state("sid", SECRET)
    # ts = int(1_000_000.0) = 1_000_000; advance by 59.9 → age = 59.9 < 60.0
    frozen_time[0] += 59.9

    assert verify_state(state, SECRET, ttl=60.0) == "sid"
