"""PKCE (Proof Key for Code Exchange) helpers — RFC 7636.

Implements the S256 code challenge method:
  1. generate_verifier()  → 32-byte URL-safe base64 string (256-bit entropy)
  2. generate_challenge(verifier) → SHA-256 of verifier, base64url-encoded (no padding)

Security properties:
  - Verifiers have 256 bits of entropy (secrets.token_urlsafe(32) → 43 chars).
  - Only S256 is supported — plain is insecure and never generated or accepted.
  - Base64url encoding follows RFC 4648 §5 (URL-safe alphabet, no padding).
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def generate_verifier() -> str:
    """Generate a PKCE code verifier with 256-bit entropy.

    Returns a 43-character URL-safe base64 string (no padding).
    Suitable for use as ``code_verifier`` in the OAuth authorization request.
    """
    return secrets.token_urlsafe(32)


def generate_challenge(verifier: str) -> str:
    """Derive the S256 code challenge from *verifier*.

    Algorithm (RFC 7636 §4.2):
      BASE64URL(SHA256(ASCII(code_verifier)))

    Returns a URL-safe base64 string without padding.

    Args:
        verifier: A code verifier previously returned by generate_verifier().

    Raises:
        ValueError: If verifier is empty.
    """
    if not verifier:
        raise ValueError("PKCE verifier must not be empty")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
