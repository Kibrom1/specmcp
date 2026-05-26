"""Unit tests for specmcp.auth.encryption."""

from __future__ import annotations

import os

import pytest

from specmcp.auth.encryption import decrypt, derive_key, encrypt


# ---------------------------------------------------------------------------
# derive_key
# ---------------------------------------------------------------------------


def test_derive_key_returns_32_bytes():
    """derive_key() always returns exactly 32 bytes."""
    key = derive_key(os.urandom(32), "test_context")
    assert len(key) == 32


def test_derive_key_same_inputs_same_output():
    """derive_key() is deterministic for the same master_key + context."""
    master = os.urandom(32)
    k1 = derive_key(master, "ctx_a")
    k2 = derive_key(master, "ctx_a")
    assert k1 == k2


def test_derive_key_different_contexts_produce_different_keys():
    """Two different context strings produce different derived keys."""
    master = os.urandom(32)
    k1 = derive_key(master, "token_store_v1")
    k2 = derive_key(master, "token_store_v2")
    assert k1 != k2


def test_derive_key_different_masters_produce_different_keys():
    """Two different master keys produce different derived keys."""
    k1 = derive_key(os.urandom(32), "ctx")
    k2 = derive_key(os.urandom(32), "ctx")
    assert k1 != k2


# ---------------------------------------------------------------------------
# encrypt / decrypt — happy path
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trips():
    """encrypt then decrypt returns the original plaintext."""
    key = derive_key(os.urandom(32), "test")
    plaintext = "Hello, OAuth token!"
    ciphertext = encrypt(plaintext, key)
    result = decrypt(ciphertext, key)
    assert result == plaintext


def test_encrypt_decrypt_empty_string():
    """Round-trip works for an empty string."""
    key = derive_key(os.urandom(32), "test")
    assert decrypt(encrypt("", key), key) == ""


def test_encrypt_decrypt_unicode():
    """Round-trip works for a JSON blob with unicode characters."""
    key = derive_key(os.urandom(32), "test")
    payload = '{"access_token": "tëst-tökën-✓", "scope": "réad"}'
    assert decrypt(encrypt(payload, key), key) == payload


def test_encrypt_produces_different_ciphertext_each_call():
    """Each call to encrypt() uses a fresh nonce — ciphertexts differ."""
    key = derive_key(os.urandom(32), "test")
    plaintext = "same plaintext"
    ct1 = encrypt(plaintext, key)
    ct2 = encrypt(plaintext, key)
    assert ct1 != ct2  # different nonces → different ciphertexts


def test_encrypt_output_length():
    """Ciphertext is nonce(12) + plaintext_bytes + tag(16)."""
    key = derive_key(os.urandom(32), "test")
    plaintext = "abc"
    ct = encrypt(plaintext, key)
    assert len(ct) == 12 + len(plaintext.encode()) + 16


# ---------------------------------------------------------------------------
# decrypt — error cases
# ---------------------------------------------------------------------------


def test_decrypt_wrong_key_raises_value_error():
    """decrypt() raises ValueError when the wrong key is used (GCM tag fails)."""
    key = derive_key(os.urandom(32), "correct")
    wrong_key = derive_key(os.urandom(32), "wrong")
    ct = encrypt("secret", key)
    with pytest.raises(ValueError, match="authentication failed|tag"):
        decrypt(ct, wrong_key)


def test_decrypt_tampered_ciphertext_raises_value_error():
    """decrypt() raises ValueError when the ciphertext body is modified."""
    key = derive_key(os.urandom(32), "test")
    ct = bytearray(encrypt("secret data", key))
    # Flip a byte in the ciphertext (after the 12-byte nonce)
    ct[20] ^= 0xFF
    with pytest.raises(ValueError):
        decrypt(bytes(ct), key)


def test_decrypt_tampered_nonce_raises_value_error():
    """decrypt() raises ValueError when the nonce is modified."""
    key = derive_key(os.urandom(32), "test")
    ct = bytearray(encrypt("secret data", key))
    ct[5] ^= 0xFF  # flip a byte inside the nonce
    with pytest.raises(ValueError):
        decrypt(bytes(ct), key)


def test_decrypt_too_short_raises_value_error():
    """decrypt() raises ValueError for ciphertext shorter than nonce + tag."""
    key = derive_key(os.urandom(32), "test")
    with pytest.raises(ValueError, match="too short"):
        decrypt(b"\x00" * 10, key)  # 10 bytes < 12 (nonce) + 16 (tag)
