#!/usr/bin/env python3
"""
token_store_rotate.py — SQLite token-store key rotation utility.

Re-encrypts every row in an existing token store database with a new AES-256-GCM
key, replacing the old key. The rotation is atomic: a temporary copy of the
database is written first, and the original is replaced only if all rows were
successfully re-encrypted.

Usage::

    python scripts/token_store_rotate.py \\
        --db ~/.specmcp/tokens.db \\
        --old-key <hex> \\
        --new-key <hex>

The --old-key and --new-key arguments accept 64-hex-digit (32-byte) keys.
If either is omitted, the script falls back to environment variables:
  TOKEN_STORE_KEY      — old (current) key
  TOKEN_STORE_KEY_NEW  — new key

Multi-scheme usage:
  When ``--token-store sqlite`` is used with multiple ``oauth2_authorization_code``
  schemes, specmcp creates one database file per scheme beside the base path::

      ~/.specmcp/tokens_myScheme.db
      ~/.specmcp/tokens_otherScheme.db

  Run this script once for each file::

      python scripts/token_store_rotate.py --db ~/.specmcp/tokens_myScheme.db ...
      python scripts/token_store_rotate.py --db ~/.specmcp/tokens_otherScheme.db ...

Exit codes:
  0  All rows rotated successfully.
  1  One or more rows failed to re-encrypt (partial failure — old DB kept).
  2  Argument error (bad key, DB not found, etc.).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def _load_key(hex_val: str | None, env_var: str, label: str) -> bytes:
    raw = hex_val or os.environ.get(env_var)
    if not raw:
        print(
            f"Error: {label} not provided. "
            f"Pass --{label.lower().replace(' ', '-')} or set {env_var}.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError:
        print(f"Error: {label} is not valid hex.", file=sys.stderr)
        sys.exit(2)
    if len(key) != 32:
        print(
            f"Error: {label} must be exactly 32 bytes (64 hex chars), got {len(key)} bytes.",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def _rotate(db_path: Path, old_key: bytes, new_key: bytes) -> int:
    """Re-encrypt all rows using new_key. Returns number of failed rows."""
    from specmcp.auth.encryption import decrypt, derive_key, encrypt

    old_derived = derive_key(old_key, "token_store_v1")
    new_derived = derive_key(new_key, "token_store_v1")

    # Work on a temp copy so the original is untouched until we're done.
    with tempfile.NamedTemporaryFile(
        dir=db_path.parent, suffix=".tmp", delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        shutil.copy2(db_path, tmp_path)

        conn = sqlite3.connect(tmp_path)
        try:
            rows = conn.execute(
                "SELECT session_id, encrypted_blob FROM oauth_tokens"
            ).fetchall()

            failures = 0
            for session_id, blob in rows:
                try:
                    plaintext = decrypt(bytes(blob), old_derived)
                    new_blob = encrypt(plaintext, new_derived)
                    conn.execute(
                        "UPDATE oauth_tokens SET encrypted_blob = ? WHERE session_id = ?",
                        (new_blob, session_id),
                    )
                except (ValueError, Exception) as exc:
                    print(
                        f"Warning: failed to re-encrypt session {session_id!r}: {exc}",
                        file=sys.stderr,
                    )
                    failures += 1

            conn.commit()
        finally:
            conn.close()

        if failures:
            print(
                f"Rotation incomplete: {failures}/{len(rows)} rows failed. "
                "Original database left unchanged.",
                file=sys.stderr,
            )
            tmp_path.unlink(missing_ok=True)
            return failures

        # Atomic replace: rename temp over original.
        tmp_path.replace(db_path)
        print(f"Rotated {len(rows)} rows in {db_path}.")
        return 0

    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        print(f"Error during rotation: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-encrypt a specmcp SQLite token store with a new AES-256-GCM key."
    )
    parser.add_argument(
        "--db",
        required=True,
        help=(
            "Path to the SQLite token store file (e.g. ~/.specmcp/tokens.db). "
            "For multi-scheme setups specmcp creates one file per scheme "
            "(e.g. tokens_myScheme.db); run this script once per file."
        ),
    )
    parser.add_argument("--old-key", help="Current 32-byte key as 64 hex chars")
    parser.add_argument("--new-key", help="New 32-byte key as 64 hex chars")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    old_key = _load_key(args.old_key, "TOKEN_STORE_KEY", "old-key")
    new_key = _load_key(args.new_key, "TOKEN_STORE_KEY_NEW", "new-key")

    if old_key == new_key:
        print("Error: old-key and new-key are identical — nothing to do.", file=sys.stderr)
        sys.exit(2)

    failures = _rotate(db_path, old_key, new_key)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
