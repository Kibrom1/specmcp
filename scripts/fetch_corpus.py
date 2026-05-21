#!/usr/bin/env python3
"""
fetch_corpus.py — Download non-vendored OpenAPI specs listed in test-corpus/manifest.yaml.

Usage:
    python scripts/fetch_corpus.py [--cache-dir test-corpus/.cache] [--verify-sha256]

Vendored specs (vendor: true) are skipped — they live in the repo already.
Non-vendored specs are fetched and written to the cache directory so the
corpus integration tests can find them without a network hit.

Exit codes:
    0   All specs fetched (or skipped) successfully.
    1   One or more fetches failed (details printed to stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve repo root relative to this script
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = REPO_ROOT / "test-corpus" / "manifest.yaml"


def _load_manifest() -> list[dict]:
    try:
        from ruamel.yaml import YAML
    except ImportError:
        print("ruamel.yaml is required. Run: pip install ruamel.yaml", file=sys.stderr)
        sys.exit(1)

    yaml = YAML(typ="safe")
    with MANIFEST_PATH.open() as f:
        data = yaml.load(f)
    return data.get("specs", [])


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fetch(url: str) -> bytes:
    try:
        import httpx
    except ImportError:
        print("httpx is required. Run: pip install httpx", file=sys.stderr)
        sys.exit(1)

    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch non-vendored corpus specs.")
    parser.add_argument(
        "--cache-dir",
        default=str(REPO_ROOT / "test-corpus" / ".cache"),
        help="Directory to write fetched specs into (default: test-corpus/.cache).",
    )
    parser.add_argument(
        "--verify-sha256",
        action="store_true",
        help="Fail if a fetched file's SHA-256 doesn't match the manifest (skipped if manifest sha256 is empty).",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    specs = _load_manifest()
    failures: list[str] = []

    for spec in specs:
        spec_id: str = spec.get("id", "unknown")
        url: str = spec.get("url", "")
        vendor: bool = spec.get("vendor", False)
        expected_sha: str = spec.get("sha256", "")

        # Vendored specs are already in the repo — skip
        if vendor:
            print(f"  skip (vendored): {spec_id}")
            continue

        # No URL — nothing to fetch
        if not url:
            print(f"  skip (no url):   {spec_id}")
            continue

        dest = cache_dir / f"{spec_id}.json"
        # Use .yaml extension if the URL ends in .yaml/.yml
        if url.lower().endswith((".yaml", ".yml")):
            dest = cache_dir / f"{spec_id}.yaml"

        print(f"  fetching:        {spec_id}  →  {url}")
        try:
            content = _fetch(url)
        except Exception as exc:
            print(f"    ERROR: {exc}", file=sys.stderr)
            failures.append(spec_id)
            continue

        # Optionally verify SHA-256
        if args.verify_sha256 and expected_sha:
            actual = _sha256(content)
            if actual != expected_sha:
                msg = f"    SHA-256 mismatch for {spec_id}: expected {expected_sha}, got {actual}"
                print(msg, file=sys.stderr)
                failures.append(spec_id)
                continue

        dest.write_bytes(content)
        print(f"    saved {len(content):,} bytes → {dest.relative_to(REPO_ROOT)}")

    print()
    if failures:
        print(f"FAILED to fetch {len(failures)} spec(s): {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Done. {sum(1 for s in specs if not s.get('vendor') and s.get('url'))} spec(s) fetched.")


if __name__ == "__main__":
    main()
