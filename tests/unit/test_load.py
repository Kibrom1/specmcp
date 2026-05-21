"""Unit tests for the Load + Parse stage."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from specmcp.core.load import (
    RawSpec,
    ResolvedSpec,
    SpecResolver,
    _detect_openapi_version,
    _parse_raw,
    load_spec,
)
from specmcp.errors import (
    SpecResolutionError,
    SpecSyntaxError,
    SpecUnsupportedError,
    SpecValidationError,
)


# ---------------------------------------------------------------------------
# Test double for SpecResolver (no real prance calls in unit tests)
# ---------------------------------------------------------------------------


class StubResolver(SpecResolver):
    """Returns a pre-built dict without calling prance."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def resolve(self, source: str) -> dict[str, Any]:
        return self._data


PETSTORE_MINIMAL: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {},
}

PETSTORE_31: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {},
}


# ---------------------------------------------------------------------------
# _parse_raw
# ---------------------------------------------------------------------------


def test_parse_raw_json():
    content = '{"openapi": "3.0.3", "info": {"title": "T", "version": "1"}}'
    data = _parse_raw(content, "spec.json")
    assert data["openapi"] == "3.0.3"


def test_parse_raw_yaml():
    content = textwrap.dedent("""\
        openapi: "3.0.3"
        info:
          title: T
          version: "1"
        paths: {}
    """)
    data = _parse_raw(content, "spec.yaml")
    assert data["openapi"] == "3.0.3"


def test_parse_raw_bad_json():
    with pytest.raises(SpecSyntaxError):
        _parse_raw("{not valid json", "spec.json")


def test_parse_raw_bad_yaml():
    with pytest.raises(SpecSyntaxError):
        _parse_raw("key: [\nbad indent", "spec.yaml")


def test_parse_raw_bytes_input():
    content = b'{"openapi": "3.0.3", "info": {"title": "T", "version": "1"}}'
    data = _parse_raw(content, "spec.json")
    assert data["openapi"] == "3.0.3"


# ---------------------------------------------------------------------------
# _detect_openapi_version
# ---------------------------------------------------------------------------


def test_detect_openapi_30():
    data = {"openapi": "3.0.3", "info": {}, "paths": {}}
    assert _detect_openapi_version(data, "spec.yaml") == "3.0"


def test_detect_openapi_31():
    data = {"openapi": "3.1.0", "info": {}, "paths": {}}
    assert _detect_openapi_version(data, "spec.yaml") == "3.1"


def test_detect_swagger_20_rejected():
    data = {"swagger": "2.0", "info": {}, "paths": {}}
    with pytest.raises(SpecUnsupportedError) as exc_info:
        _detect_openapi_version(data, "spec.yaml")
    assert "v1.1" in exc_info.value.message  # tells user about the plan


def test_detect_openapi_field_swagger_version_rejected():
    data = {"openapi": "2.0.0", "info": {}, "paths": {}}
    with pytest.raises(SpecUnsupportedError):
        _detect_openapi_version(data, "spec.yaml")


def test_detect_missing_openapi_field():
    data = {"info": {}, "paths": {}}
    with pytest.raises(SpecValidationError):
        _detect_openapi_version(data, "spec.yaml")


def test_detect_unknown_version():
    data = {"openapi": "4.0.0", "info": {}, "paths": {}}
    with pytest.raises(SpecUnsupportedError):
        _detect_openapi_version(data, "spec.yaml")


# ---------------------------------------------------------------------------
# load_spec — local file (stub resolver, no prance)
# ---------------------------------------------------------------------------


def test_load_spec_local_json(tmp_path):
    spec_file = tmp_path / "petstore.json"
    spec_file.write_text(json.dumps(PETSTORE_MINIMAL))
    stub = StubResolver(PETSTORE_MINIMAL)
    raw, resolved = load_spec(spec_file, resolver=stub)
    assert isinstance(raw, RawSpec)
    assert isinstance(resolved, ResolvedSpec)
    assert resolved.openapi_version == "3.0"
    assert raw.source == str(spec_file)


def test_load_spec_31(tmp_path):
    spec_file = tmp_path / "spec31.json"
    spec_file.write_text(json.dumps(PETSTORE_31))
    stub = StubResolver(PETSTORE_31)
    _, resolved = load_spec(spec_file, resolver=stub)
    assert resolved.openapi_version == "3.1"


def test_load_spec_swagger_rejected(tmp_path):
    swagger_spec = {"swagger": "2.0", "info": {"title": "T", "version": "1"}, "paths": {}}
    spec_file = tmp_path / "swagger.json"
    spec_file.write_text(json.dumps(swagger_spec))
    stub = StubResolver(swagger_spec)
    with pytest.raises(SpecUnsupportedError) as exc_info:
        load_spec(spec_file, resolver=stub)
    assert "v1.1" in exc_info.value.message
    assert "openapi-spec-converter" in exc_info.value.message


def test_load_spec_file_not_found():
    with pytest.raises(SpecResolutionError, match="not found"):
        load_spec("/tmp/definitely_does_not_exist_specmcp_xyz.json")


def test_load_spec_bad_syntax(tmp_path):
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text("key: [\nbad yaml here: [")
    stub = StubResolver({})
    with pytest.raises(SpecSyntaxError):
        load_spec(spec_file, resolver=stub)


# ---------------------------------------------------------------------------
# SpecResolver interface contract (test double)
# ---------------------------------------------------------------------------


def test_spec_resolver_is_decoupled(tmp_path):
    """SpecResolver can be replaced by any object with a resolve() method."""

    class MyResolver:
        def resolve(self, source: str) -> dict[str, Any]:
            return PETSTORE_MINIMAL

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(PETSTORE_MINIMAL))

    # This must work without importing prance at all
    raw, resolved = load_spec(spec_file, resolver=MyResolver())  # type: ignore[arg-type]
    assert resolved.openapi_version == "3.0"
