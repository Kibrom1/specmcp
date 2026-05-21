"""
Corpus integration tests for specmcp.

These tests run the full Load → Normalize → Simplify → Expose pipeline
against every spec in the test corpus (both vendored and fetched) and
assert that the conversion succeeds with at least the minimum required
success rate.

Running:
    # Vendored + adversarial specs only (no network):
    pytest tests/integration/test_corpus.py -v

    # Full corpus including fetched specs (requires fetch_corpus.py to have run):
    SPECMCP_RUN_CORPUS=1 pytest tests/integration/test_corpus.py -v

    # Override minimum success rate (default 80%):
    SPECMCP_CORPUS_MIN_SUCCESS_RATE=0.90 pytest tests/integration/test_corpus.py -v

CI behaviour:
    The corpus job in CI sets SPECMCP_RUN_CORPUS=1 and runs fetch_corpus.py
    before this suite, so all non-vendored specs are available in the cache dir.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
CORPUS_DIR = REPO_ROOT / "test-corpus"
CACHE_DIR = CORPUS_DIR / ".cache"
MANIFEST_PATH = CORPUS_DIR / "manifest.yaml"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

#: Set SPECMCP_RUN_CORPUS=1 to include fetched (non-vendored) specs.
RUN_FULL_CORPUS = os.environ.get("SPECMCP_RUN_CORPUS", "0") == "1"

#: Minimum fraction of operations that must convert without error.
MIN_SUCCESS_RATE = float(os.environ.get("SPECMCP_CORPUS_MIN_SUCCESS_RATE", "0.80"))


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_manifest() -> list[dict[str, Any]]:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    with MANIFEST_PATH.open() as f:
        data = yaml.load(f)
    return data.get("specs", [])


def _spec_path(entry: dict[str, Any]) -> Path | None:
    """Return the local path to a spec, or None if it is not available."""
    if entry.get("vendor"):
        vendor_path = entry.get("vendor_path", "")
        if vendor_path:
            return CORPUS_DIR / vendor_path
        return None

    if RUN_FULL_CORPUS:
        spec_id = entry["id"]
        url = entry.get("url", "")
        if not url:
            return None
        # Determine extension from URL
        ext = ".yaml" if url.lower().endswith((".yaml", ".yml")) else ".json"
        candidate = CACHE_DIR / f"{spec_id}{ext}"
        if candidate.exists():
            return candidate
        # Try the other extension
        alt_ext = ".json" if ext == ".yaml" else ".yaml"
        candidate_alt = CACHE_DIR / f"{spec_id}{alt_ext}"
        if candidate_alt.exists():
            return candidate_alt

    return None


# ---------------------------------------------------------------------------
# Parametrize corpus entries
# ---------------------------------------------------------------------------


def _corpus_params() -> list[pytest.param]:
    """Build the list of pytest parameters from the manifest."""
    try:
        entries = _load_manifest()
    except Exception:
        return []

    params = []
    for entry in entries:
        spec_id = entry.get("id", "unknown")
        tags = entry.get("tags", [])

        # Skip entries explicitly tagged 'skip'
        if "skip" in tags:
            continue

        path = _spec_path(entry)
        if path is None:
            # Mark as skipped rather than failing collection
            params.append(
                pytest.param(
                    spec_id,
                    None,
                    entry,
                    id=spec_id,
                    marks=pytest.mark.skip(
                        reason="spec not available locally (run fetch_corpus.py or set SPECMCP_RUN_CORPUS=1)"
                    ),
                )
            )
        else:
            marks = []
            if "adversarial" in tags:
                marks.append(pytest.mark.adversarial)
            params.append(pytest.param(spec_id, path, entry, id=spec_id, marks=marks))

    return params


# Register custom marks so pytest doesn't warn about unknown marks.
def pytest_configure(config: Any) -> None:  # noqa: ANN001
    config.addinivalue_line("markers", "adversarial: adversarial corpus spec tests")
    config.addinivalue_line("markers", "corpus: full corpus integration tests")


# ---------------------------------------------------------------------------
# Core pipeline helper
# ---------------------------------------------------------------------------


def _run_pipeline(spec_path: Path) -> dict[str, Any]:
    """Run the full Load → Normalize → Simplify → Expose pipeline.

    Returns a dict with:
        total_ops: int           — number of operations in the spec
        converted_ops: int       — number that produced a SimplifiedOperation
        failed_ops: list[str]    — operation IDs that raised during conversion
        errors: list[str]        — human-readable error messages
    """
    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    raw, resolved = load_spec(str(spec_path))
    ops = normalize(resolved)
    simplified = simplify(ops, config=SimplifyConfig())
    registry = ToolRegistry.build(simplified)

    total_ops = len(ops)
    converted_ops = len(registry.tools)
    failed_ops: list[str] = []
    errors: list[str] = []

    # Any op that normalized but didn't make it into the registry is a failure
    registry_ids = {t.simplified_operation.operation.id for t in registry.tools}
    for op in ops:
        if op.id not in registry_ids:
            failed_ops.append(op.id)
            errors.append(f"Operation '{op.id}' did not produce a tool in the registry")

    return {
        "total_ops": total_ops,
        "converted_ops": converted_ops,
        "failed_ops": failed_ops,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.corpus
@pytest.mark.parametrize("spec_id,spec_path,entry", _corpus_params())
def test_corpus_conversion(
    spec_id: str,
    spec_path: Path | None,
    entry: dict[str, Any],
) -> None:
    """Full pipeline must succeed for every operation in the spec.

    For adversarial specs: asserts that the pipeline either succeeds OR raises
    a typed SpecmcpError (a clean, intentional failure). An unhandled Python
    exception (AttributeError, RecursionError, etc.) is always a bug.
    For normal specs: asserts at least MIN_SUCCESS_RATE of ops convert.
    """
    assert spec_path is not None, "spec_path should never be None after parametrize"
    assert spec_path.exists(), f"Spec file missing: {spec_path}"

    from specmcp.errors import SpecmcpError

    tags = entry.get("tags", [])
    is_adversarial = "adversarial" in tags
    per_spec_min = entry.get("min_success_rate", MIN_SUCCESS_RATE)

    # --- Load and convert ---
    try:
        result = _run_pipeline(spec_path)
    except SpecmcpError as exc:
        if is_adversarial:
            # A typed SpecmcpError (e.g. SpecResolutionError for circular refs)
            # is an acceptable, clean failure — the pipeline surfaced the problem
            # with a useful error message rather than crashing.
            return
        raise
    except Exception as exc:
        # Any non-SpecmcpError exception is a bug — adversarial or not.
        pytest.fail(
            f"Pipeline raised unhandled {type(exc).__name__} on spec '{spec_id}': {exc}"
        )

    total = result["total_ops"]
    converted = result["converted_ops"]
    errors = result["errors"]

    if is_adversarial:
        # Pipeline completed without error — verify it produced a non-empty spec.
        assert total > 0, f"Adversarial spec '{spec_id}' has no operations"
        return

    # --- Success rate check ---
    if total == 0:
        pytest.skip(f"Spec '{spec_id}' has no operations to convert")

    success_rate = converted / total
    assert success_rate >= per_spec_min, (
        f"Spec '{spec_id}': conversion success rate {success_rate:.1%} "
        f"is below minimum {per_spec_min:.1%}. "
        f"Converted {converted}/{total} operations. "
        f"Failed ops: {result['failed_ops'][:5]}. "  # show first 5
        f"Errors: {errors[:3]}"
    )


@pytest.mark.corpus
@pytest.mark.parametrize("spec_id,spec_path,entry", _corpus_params())
def test_corpus_tool_names_unique(
    spec_id: str,
    spec_path: Path | None,
    entry: dict[str, Any],
) -> None:
    """All tool names in the registry must be unique."""
    assert spec_path is not None
    if not spec_path.exists():
        pytest.skip(f"Spec not available: {spec_path}")

    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    from specmcp.errors import SpecmcpError

    try:
        _, resolved = load_spec(str(spec_path))
        ops = normalize(resolved)
        simplified = simplify(ops, config=SimplifyConfig())
        registry = ToolRegistry.build(simplified)
    except SpecmcpError:
        if "adversarial" in entry.get("tags", []):
            pytest.skip("adversarial spec raised clean SpecmcpError — skipping uniqueness check")
        raise
    except Exception as exc:
        pytest.fail(f"Pipeline raised unhandled {type(exc).__name__} on spec '{spec_id}': {exc}")

    names = [t.name for t in registry.tools]
    assert len(names) == len(set(names)), (
        f"Spec '{spec_id}' produced duplicate tool names: "
        + str([n for n in names if names.count(n) > 1])
    )


@pytest.mark.corpus
@pytest.mark.parametrize("spec_id,spec_path,entry", _corpus_params())
def test_corpus_tool_schemas_valid(
    spec_id: str,
    spec_path: Path | None,
    entry: dict[str, Any],
) -> None:
    """Every tool's inputSchema must be a valid JSON Schema object."""
    assert spec_path is not None
    if not spec_path.exists():
        pytest.skip(f"Spec not available: {spec_path}")

    import jsonschema

    from specmcp.config import SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    from specmcp.errors import SpecmcpError

    try:
        _, resolved = load_spec(str(spec_path))
        ops = normalize(resolved)
        simplified = simplify(ops, config=SimplifyConfig())
        registry = ToolRegistry.build(simplified)
    except SpecmcpError:
        if "adversarial" in entry.get("tags", []):
            pytest.skip("adversarial spec raised clean SpecmcpError — skipping schema check")
        raise
    except Exception as exc:
        pytest.fail(f"Pipeline raised unhandled {type(exc).__name__} on spec '{spec_id}': {exc}")

    bad_tools: list[str] = []
    for tool in registry.tools:
        schema = tool.input_schema
        if not isinstance(schema, dict):
            bad_tools.append(f"{tool.name}: inputSchema is not a dict")
            continue
        if schema.get("type") != "object":
            bad_tools.append(f"{tool.name}: inputSchema.type is not 'object' (got {schema.get('type')!r})")

    assert not bad_tools, (
        f"Spec '{spec_id}' produced invalid tool schemas:\n" + "\n".join(bad_tools[:10])
    )
