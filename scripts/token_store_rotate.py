#!/usr/bin/env python3
"""
Token store key rotation script.

Reads all rows in the SQLite token store, decrypts with the OLD key,
re-encrypts with the NEW key, and writes back atomically.

Usage:
    TOKEN_STORE_KEY=<old-hex>  TOKEN_STORE_KEY_NEW=<new-hex> \\
        python scripts/token_store_rotate.py --db ~/.specmcp/tokens.db

Or explicitly:
    python scripts/token_store_rotate.py \\
        --db ~/.specmcp/tokens.db \\
        --old-key "$(echo $TOKEN_STORE_KEY)" \\
        --new-key "$(echo $TOKEN_STORE_KEY_NEW)"

Key format: 64-character hex string (32 bytes = 256 bits).
Generate a new key with:
    python -c "import secrets; print(secrets.token_hex(32))"

Exit codes:
    0  — rotation completed successfully
    1  — one or more rows failed to decrypt/re-encrypt (old key retained for those rows)
    2  — argument / configuration error

Procedure:
    1. Set TOKEN_STORE_KEY_NEW to the new key value.
    2. Run this script.
    3. On success, set TOKEN_STORE_KEY=<new-value> and remove TOKEN_STORE_KEY_NEW.
    4. Restart specmcp.

If the script exits 1 (partial failure), the database is left in a mixed state:
    rows that succeeded are encrypted with the new key; failed rows retain the old key.
    Restart with the old key and investigate the errors before retrying.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


def _resolve_key(hex_str: str, arg_name: str) -> bytes:
    hex_str = hex_str.strip()
    try:
        key = bytes.fromhex(hex_str)
    except ValueError as exc:
        print(f"Error: {arg_name} is not valid hex: {exc}", file=sys.stderr)
        sys.exit(2)
    if len(key) != 32:
        print(
            f"Error: {arg_name} must be 64 hex characters (32 bytes). "
            f"Got {len(key)} bytes.",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def _get_key_from_env_or_arg(env_var: str, arg_value: str | None, label: str) -> bytes:
    if arg_value:
        return _resolve_key(arg_value, label)
    raw = os.environ.get(env_var)
    if not raw:
        print(
            f"Error: provide --{label.lower().replace(' ', '-')} "
            f"or set {env_var} environment variable.",
            file=sys.stderr,
        )
        sys.exit(2)
    return _resolve_key(raw, env_var)


def rotate(db_path: Path, old_key: bytes, new_key: bytes) -> int:
    """Rotate all rows in *db_path* from *old_key* to *new_key*.

    Returns the number of rows that failed to process (0 = full success).
    Uses a single SQLite transaction — all rows are written or none are
    (on crash mid-transaction the original data is preserved).
    """
    # Lazy import so this script can be run standalone without the full
    # specmcp package installed (as long as cryptography is available).
    try:
        from specmcp.auth.encryption import decrypt, encrypt, derive_key
    except ImportError:
        # Fall back to inline implementation for standalone use
        import base64
        import hashlib
        import hmac

        def derive_key(master_key: bytes, context: str) -> bytes:  # type: ignore[misc]
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            from cryptography.hazmat.primitives import hashes
            hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context.encode())
            return hkdf.derive(master_key)

        def encrypt(plaintext: str, key: bytes) -> bytes:  # type: ignore[misc]
            import os
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(12)
            ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
            return nonce + ct

        def decrypt(ciphertext: bytes, key: bytes) -> str:  # type: ignore[misc]
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.exceptions import InvalidTag
            nonce, ct = ciphertext[:12], ciphertext[12:]
            try:
                return AESGCM(key).decrypt(nonce, ct, None).decode()
            except InvalidTag as e:
                raise ValueError("AES-GCM tag verification failed") from e

    context = "token_store_v1"
    old_derived = derive_key(old_key, context)
    new_derived = derive_key(new_key, context)

    conn = sqlite3.connect(db_path)
    failures = 0

    try:
        rows = conn.execute(
            "SELECT session_id, encrypted_blob FROM oauth_tokens"
        ).fetchall()

        print(f"Found {len(rows)} row(s) to rotate.", flush=True)
        if not rows:
            print("Nothing to do.")
            return 0

        updates: list[tuple[bytes, float, str]] = []

        for session_id, blob in rows:
            try:
                plaintext = decrypt(bytes(blob), old_derived)
                new_blob = encrypt(plaintext, new_derived)
                updates.append((new_blob, time.time(), session_id))
            except Exception as exc:
                print(
                    f"  FAIL  session={session_id!r}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                failures += 1

        if failures:
            print(
                f"\n{failures} row(s) failed. Database NOT modified for failed rows.",
                file=sys.stderr,
            )

        # Write successful rows atomically
        successful = len(updates)
        if successful > 0:
            with conn:  # transaction
                conn.executemany(
                    "UPDATE oauth_tokens SET encrypted_blob=?, updated_at=? WHERE session_id=?",
                    updates,
                )
            print(f"Rotated {successful} row(s) successfully.")
        else:
            print("No rows updated (all failed).", file=sys.stderr)

    finally:
        conn.close()

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate token store encryption keys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        required=True,
        metavar="PATH",
        help="Path to the SQLite token store file.",
    )
    parser.add_argument(
        "--old-key",
        metavar="HEX",
        help="Old encryption key (64 hex chars). Falls back to TOKEN_STORE_KEY env var.",
    )
    parser.add_argument(
        "--new-key",
        metavar="HEX",
        help="New encryption key (64 hex chars). Falls back to TOKEN_STORE_KEY_NEW env var.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    old_key = _get_key_from_env_or_arg("TOKEN_STORE_KEY", args.old_key, "old-key")
    new_key = _get_key_from_env_or_arg("TOKEN_STORE_KEY_NEW", args.new_key, "new-key")

    if old_key == new_key:
        print("Error: old-key and new-key are identical — nothing to do.", file=sys.stderr)
        sys.exit(2)

    failures = rotate(db_path, old_key, new_key)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
