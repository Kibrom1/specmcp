"""AES-256-GCM encryption helpers for the token store.

All functions use the ``cryptography`` library (PyCA). The key derivation
function is HKDF-SHA256, producing a 256-bit derived key from a master key
and a context string. This isolates token-store keys from other uses of the
same master key.

Wire format for ``encrypt`` output::

    [ nonce (12 bytes) | ciphertext (variable) | GCM tag (16 bytes) ]

The tag is appended by ``AESGCM.encrypt()`` automatically.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_NONCE_BYTES = 12   # 96-bit nonce — recommended for AES-GCM
_TAG_BYTES = 16     # GCM authentication tag


def derive_key(master_key: bytes, context: str) -> bytes:
    """Derive a 256-bit key from *master_key* using HKDF-SHA256.

    Different *context* strings produce independent keys, so the token store
    can use a dedicated sub-key even if the master key is shared.

    Args:
        master_key: Raw bytes of any length (recommended: >= 32 bytes).
        context: Unique ASCII string identifying this key's purpose.

    Returns:
        32-byte derived key.
    """
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=context.encode("ascii"),
    )
    return hkdf.derive(master_key)


def encrypt(plaintext: str, key: bytes) -> bytes:
    """Encrypt *plaintext* with AES-256-GCM using a random nonce.

    Each call generates a fresh 12-byte random nonce, so identical plaintexts
    produce different ciphertexts. The nonce is prepended to the output.

    Args:
        plaintext: UTF-8 string to encrypt.
        key: 32-byte AES key (e.g. from ``derive_key``).

    Returns:
        Bytes: nonce (12) + ciphertext + tag (16).
    """
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext_and_tag


def decrypt(ciphertext: bytes, key: bytes) -> str:
    """Decrypt *ciphertext* and return the plaintext string.

    Args:
        ciphertext: Output of ``encrypt`` — nonce + ciphertext + tag.
        key: 32-byte AES key matching the one used for encryption.

    Returns:
        Decrypted UTF-8 string.

    Raises:
        ValueError: if the input is too short, the tag fails verification
            (wrong key, tampered data), or the plaintext is not valid UTF-8.
    """
    min_length = _NONCE_BYTES + _TAG_BYTES  # 28 bytes — just nonce + tag, no body
    if len(ciphertext) < min_length:
        raise ValueError(
            f"Ciphertext is too short ({len(ciphertext)} bytes); "
            f"minimum is {min_length} bytes (nonce + tag)."
        )

    nonce = ciphertext[:_NONCE_BYTES]
    body = ciphertext[_NONCE_BYTES:]

    aesgcm = AESGCM(key)
    try:
        plaintext_bytes = aesgcm.decrypt(nonce, body, None)
    except InvalidTag as exc:
        raise ValueError("Decryption authentication failed — wrong key or tampered data.") from exc

    return plaintext_bytes.decode("utf-8")
