"""OAuth state parameter helpers for CSRF protection.

The ``state`` parameter is a signed token that encodes the originating
session_id and a timestamp. It is verified on the callback to ensure the
callback belongs to the session that initiated the flow, preventing CSRF.

Format (URL-safe base64, no padding):
  BASE64URL( session_id_len(2 bytes big-endian)
           | session_id(UTF-8)
           | timestamp(8 bytes big-endian, seconds since epoch)
           | HMAC-SHA256(secret, above payload)[0:16] )

Design decisions:
  - HMAC-SHA256 over the full payload (not just session_id) so the timestamp
    is also authenticated, preventing replay of expired states.
  - 16-byte (128-bit) MAC truncation is sufficient for a single-use CSRF token
    and keeps the state string short enough for URL parameters.
  - Length-prefix encoding prevents session_id/timestamp boundary ambiguity.
  - TTL is enforced by the caller (verify_state) using the embedded timestamp.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import struct
import time


# Size constants
_SESSION_LEN_BYTES = 2   # big-endian uint16 for session_id length
_TIMESTAMP_BYTES = 8     # big-endian uint64 (Unix seconds)
_MAC_BYTES = 16          # first 16 bytes of HMAC-SHA256


def make_state(session_id: str, secret: str | bytes) -> str:
    """Create a signed state token for *session_id*.

    Args:
        session_id: The session identifier to embed.
        secret: HMAC secret key (str is UTF-8 encoded; bytes used directly).

    Returns:
        URL-safe base64 state string (no padding).

    Raises:
        ValueError: If session_id is empty or exceeds 65535 bytes.
    """
    sid_bytes = session_id.encode("utf-8")
    if not sid_bytes:
        raise ValueError("session_id must not be empty")
    if len(sid_bytes) > 65535:
        raise ValueError("session_id too long (max 65535 bytes)")

    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    ts = int(time.time())

    # Payload: length-prefix | session_id | timestamp
    payload = struct.pack(">H", len(sid_bytes)) + sid_bytes + struct.pack(">Q", ts)

    mac = hmac.new(key, payload, hashlib.sha256).digest()[:_MAC_BYTES]
    token = payload + mac
    return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")


def verify_state(
    state: str,
    secret: str | bytes,
    *,
    ttl: float = 600.0,
) -> str:
    """Verify *state* and return the embedded session_id.

    Args:
        state: The state string returned by make_state().
        secret: The same secret used to create the state.
        ttl: Maximum age of the state in seconds (default 600 = 10 minutes).

    Returns:
        The session_id embedded in the state.

    Raises:
        ValueError: If the state is malformed, MAC is invalid, or expired.
    """
    key = secret.encode("utf-8") if isinstance(secret, str) else secret

    # Decode (add padding back for standard base64 decoder)
    padding = (4 - len(state) % 4) % 4
    try:
        raw = base64.urlsafe_b64decode(state + "=" * padding)
    except Exception as exc:
        raise ValueError(f"state is not valid base64: {exc}") from exc

    min_len = _SESSION_LEN_BYTES + 1 + _TIMESTAMP_BYTES + _MAC_BYTES  # 1-byte session_id minimum
    if len(raw) < min_len:
        raise ValueError("state token is too short")

    # Parse session_id length
    (sid_len,) = struct.unpack_from(">H", raw, 0)
    payload_end = _SESSION_LEN_BYTES + sid_len + _TIMESTAMP_BYTES

    if len(raw) < payload_end + _MAC_BYTES:
        raise ValueError("state token is truncated")

    payload = raw[:payload_end]
    received_mac = raw[payload_end: payload_end + _MAC_BYTES]

    # Constant-time MAC verification
    expected_mac = hmac.new(key, payload, hashlib.sha256).digest()[:_MAC_BYTES]
    if not hmac.compare_digest(expected_mac, received_mac):
        raise ValueError("state MAC verification failed")

    # Extract session_id
    sid_start = _SESSION_LEN_BYTES
    session_id = raw[sid_start: sid_start + sid_len].decode("utf-8")

    # Check timestamp
    (ts,) = struct.unpack_from(">Q", raw, _SESSION_LEN_BYTES + sid_len)
    age = time.time() - ts
    if age < 0 or age > ttl:
        raise ValueError(f"state token expired (age={age:.0f}s, ttl={ttl}s)")

    return session_id
