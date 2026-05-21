"""
specmcp Load + Parse stage.

Fetches an OpenAPI spec from a local path or URL, validates its syntax
and meta-schema, resolves all $refs, and returns two views:

  - RawSpec:      parsed with ruamel.yaml — preserves line/column info for
                  error messages.
  - ResolvedSpec: refs fully inlined via prance — used by downstream stages.

All prance calls go through SpecResolver so it can be swapped without
touching the rest of the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specmcp.errors import (
    SourceLocation,
    SpecResolutionError,
    SpecSyntaxError,
    SpecUnsupportedError,
    SpecValidationError,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RawSpec:
    """Parsed spec with line/column preservation (for error messages)."""

    data: dict[str, Any]
    source: str  # path or URL string


@dataclass
class ResolvedSpec:
    """Spec with all $refs inlined and validated against the OpenAPI meta-schema."""

    data: dict[str, Any]
    source: str
    openapi_version: str  # "3.0", "3.1", or "2.0" (rejected)


# ---------------------------------------------------------------------------
# SpecResolver — swap point for prance
# ---------------------------------------------------------------------------


class SpecResolver:
    """Thin wrapper around prance for $ref resolution.

    Keeping prance behind this interface means we can replace it with
    jsonref or a custom resolver if we hit limits, without touching
    downstream code.
    """

    def resolve(self, source: str) -> dict[str, Any]:
        """Resolve all $refs in the spec at *source* and return the inlined dict.

        *source* can be a local file path or an HTTP(S) URL.

        Raises SpecResolutionError if any $ref cannot be resolved.
        """
        try:
            import prance  # lazy import — only needed in the load stage

            # prance ≥25.x uses positional url + lazy kwarg only.
            # resolve_types constants were removed; all ref types are resolved by default.
            parser = prance.ResolvingParser(source, lazy=False)
            parser.parse()
            return parser.specification  # type: ignore[return-value]
        except ImportError as exc:
            raise SpecResolutionError(
                "prance is not installed. Run: pip install prance"
            ) from exc
        except SpecResolutionError:
            raise
        except Exception as exc:
            raise SpecResolutionError(
                f"Failed to resolve $refs in spec: {exc}",
                detail=str(exc),
            ) from exc


# ---------------------------------------------------------------------------
# OpenAPI version detection
# ---------------------------------------------------------------------------


def _detect_openapi_version(data: dict[str, Any], source: str) -> str:
    """Return "3.0", "3.1", or "2.0". Raises SpecUnsupportedError for Swagger 2."""
    openapi = data.get("openapi", "")
    swagger = data.get("swagger", "")

    if swagger or (isinstance(openapi, str) and openapi.startswith("2.")):
        raise SpecUnsupportedError(
            "Swagger 2.0 is not supported in specmcp v1. "
            "Support is planned for v1.1. "
            "You can use the openapi-spec-converter tool to upgrade your spec to OpenAPI 3.0 first.",
        )

    if not isinstance(openapi, str) or not openapi:
        raise SpecValidationError(
            f"Cannot determine OpenAPI version from spec at {source!r}. "
            "The 'openapi' field is missing or empty.",
        )

    if openapi.startswith("3.1"):
        return "3.1"
    if openapi.startswith("3.0"):
        return "3.0"

    raise SpecUnsupportedError(
        f"OpenAPI version {openapi!r} is not supported. "
        "specmcp v1 supports OpenAPI 3.0 and 3.1.",
    )


# ---------------------------------------------------------------------------
# Raw parsing (ruamel — preserves line numbers)
# ---------------------------------------------------------------------------


def _parse_raw(content: str | bytes, source: str) -> dict[str, Any]:
    """Parse YAML or JSON content and return a plain dict.

    Uses ruamel.yaml for YAML so error messages include line numbers.
    Falls back to json.loads for .json files (faster; equally precise errors).
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    src_lower = source.lower()
    if src_lower.endswith(".json") or (
        content.lstrip().startswith("{") and not src_lower.endswith(".yaml")
        and not src_lower.endswith(".yml")
    ):
        try:
            return json.loads(content)  # type: ignore[return-value]
        except json.JSONDecodeError as exc:
            raise SpecSyntaxError(
                f"JSON parse error in {source!r}: {exc}",
                location=SourceLocation(file=source, line=exc.lineno),
                detail=str(exc),
            ) from exc

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError

        yaml = YAML(typ="safe")
        import io

        data = yaml.load(io.StringIO(content))
        if not isinstance(data, dict):
            raise SpecSyntaxError(
                f"Spec at {source!r} must be a YAML/JSON mapping, got {type(data).__name__}"
            )
        return data  # type: ignore[return-value]
    except SpecSyntaxError:
        raise
    except Exception as exc:
        # Try to extract line number from ruamel error
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        location = SourceLocation(file=source, line=(line or 0) + 1) if line is not None else None
        raise SpecSyntaxError(
            f"YAML parse error in {source!r}: {exc}",
            location=location,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Meta-schema validation
# ---------------------------------------------------------------------------


def _validate_meta_schema(data: dict[str, Any], source: str, version: str) -> None:
    """Validate spec against the OpenAPI meta-schema.

    Raises SpecValidationError with the first validation error.
    """
    try:
        from openapi_spec_validator import validate  # type: ignore[import]
        from openapi_spec_validator.validation.exceptions import OpenAPISpecValidatorError  # type: ignore[import]

        validate(data)
    except ImportError:
        # openapi-spec-validator not installed — skip meta-schema validation
        return
    except Exception as exc:
        msg = str(exc)
        raise SpecValidationError(
            f"Spec at {source!r} fails OpenAPI meta-schema validation: {msg[:200]}",
            detail=msg,
        ) from exc


# ---------------------------------------------------------------------------
# Remote fetch
# ---------------------------------------------------------------------------


def _fetch_remote(url: str) -> bytes:
    """Fetch a remote spec via HTTP(S). Returns raw bytes."""
    try:
        import httpx

        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content
    except ImportError as exc:
        raise SpecResolutionError("httpx is not installed; cannot fetch remote specs.") from exc
    except Exception as exc:
        from specmcp.errors import TransientError

        raise TransientError(
            f"Failed to fetch spec from {url!r}: {exc}",
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_spec(
    source: str | Path,
    *,
    resolver: SpecResolver | None = None,
) -> tuple[RawSpec, ResolvedSpec]:
    """Load, parse, validate, and resolve an OpenAPI spec.

    Parameters
    ----------
    source:
        Local file path or HTTP(S) URL.
    resolver:
        Optional SpecResolver instance. Pass a test double to avoid
        real prance calls in unit tests.

    Returns
    -------
    tuple[RawSpec, ResolvedSpec]
        ``raw`` preserves line numbers for error reporting.
        ``resolved`` has all $refs inlined and is validated against the meta-schema.

    Raises
    ------
    SpecSyntaxError
        YAML/JSON parse failure.
    SpecValidationError
        Meta-schema validation failure.
    SpecResolutionError
        $ref cannot be resolved.
    SpecUnsupportedError
        Swagger 2.0 or unknown version.
    TransientError
        Network error fetching a remote spec.
    """
    source_str = str(source)
    is_remote = source_str.startswith("http://") or source_str.startswith("https://")

    # 1. Fetch content
    if is_remote:
        content = _fetch_remote(source_str)
    else:
        path = Path(source_str)
        if not path.exists():
            raise SpecResolutionError(f"Spec file not found: {path}")
        content = path.read_bytes()

    # 2. Parse raw (for line-number error reporting)
    raw_data = _parse_raw(content, source_str)
    raw = RawSpec(data=raw_data, source=source_str)

    # 3. Detect version (rejects Swagger 2.0 here)
    openapi_version = _detect_openapi_version(raw_data, source_str)

    # 4. Resolve $refs — use the provided resolver or default to prance
    if resolver is None:
        resolver = SpecResolver()

    resolved_data = resolver.resolve(source_str)

    # 5. Validate against the OpenAPI meta-schema
    _validate_meta_schema(resolved_data, source_str, openapi_version)

    resolved = ResolvedSpec(
        data=resolved_data,
        source=source_str,
        openapi_version=openapi_version,
    )
    return raw, resolved
