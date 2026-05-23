"""
AES-256-GCM token encryption for SqliteTokenStore.

All functions are pure (no I/O, no config imports) and operate on raw bytes.
This makes them easy to unit-test in isolation and reuse across contexts.

Key derivation:
  derive_key(master_key, context) uses HKDF-SHA256 to derive a 32-byte AES key
  from a master key. Different context strings produce different derived keys,
  allowing future key versioning without changing the master key.

Encryption format (encrypt → bytes):
  | 12-byte nonce | ciphertext + 16-byte GCM tag |

  The nonce is randomly generated per call; no nonce reuse is possible.
  The GCM tag is appended by the `cryptography` library automatically.

Decryption:
  decrypt(ciphertext, key) expects the same format produced by encrypt().
  Raises ValueError if the GCM tag check fails (tampered ciphertext).
"""

from __future__ import annotations

import os


def derive_key(master_key: bytes, context: str) -> bytes:
    """Derive a 32-byte AES-256 key from *master_key* using HKDF-SHA256.

    Args:
        master_key: The master key bytes (e.g. decoded from TOKEN_STORE_KEY).
        context: A short, unique label for the derived key's purpose.
            Different contexts produce different keys — use versioned strings
            like "token_store_v1" to allow future key rotation without changing
            the master key.

    Returns:
        32 bytes suitable for AES-256-GCM.
    """
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=context.encode(),
    )
    return hkdf.derive(master_key)


def encrypt(plaintext: str, key: bytes) -> bytes:
    """AES-256-GCM encrypt *plaintext* with *key*.

    A fresh 12-byte nonce is generated on every call — callers should never
    pass the same (key, nonce) pair twice, but this function guarantees it
    by using os.urandom(12).

    Returns:
        Bytes in the format: ``nonce (12) || ciphertext+tag``.
        The GCM authentication tag is the last 16 bytes of ciphertext+tag.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return nonce + ciphertext_with_tag


def decrypt(ciphertext: bytes, key: bytes) -> str:
    """AES-256-GCM decrypt *ciphertext* with *key*.

    Args:
        ciphertext: Bytes produced by :func:`encrypt` — nonce followed by
            ciphertext+tag.
        key: The same 32-byte key used for encryption.

    Returns:
        The original plaintext string.

    Raises:
        ValueError: If the GCM authentication tag check fails, indicating the
            ciphertext was tampered with or the wrong key was used.
        ValueError: If the ciphertext is too short to contain a nonce + tag.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    if len(ciphertext) < 12 + 16:  # nonce + minimum tag
        raise ValueError(
            f"Ciphertext too short ({len(ciphertext)} bytes); "
            "expected at least 28 bytes (12-byte nonce + 16-byte tag)."
        )

    nonce = ciphertext[:12]
    ct_with_tag = ciphertext[12:]
    aesgcm = AESGCM(key)

    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ct_with_tag, None)
    except InvalidTag as exc:
        raise ValueError(
            "AES-GCM authentication failed — ciphertext is tampered or wrong key."
        ) from exc

    return plaintext_bytes.decode()
