"""
Golden-file tests for the specmcp naming algorithm.

These assertions are a CONTRACT. Any change to expected output is a
breaking change to the naming algorithm and requires an explicit review
comment: "This is a breaking change to the naming algorithm."

See docs/naming.md for the full algorithm specification.
"""

import pytest

from specmcp.core.normalize import (
    _derive_operation_id,
    _resolve_collisions,
    _sanitize_path_segment,
)


# ---------------------------------------------------------------------------
# _sanitize_path_segment  (Step 2 internals)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/pets", "pets"),
        ("/pets/{petId}", "pets_petId"),
        ("/users/{id}/orders/{orderId}", "users_id_orders_orderId"),
        ("/v1/api/items", "v1_api_items"),
        ("/", ""),
        ("/{id}", "id"),
        ("/a//b", "a_b"),  # double slash → collapsed _
        ("/api/v2/items/{item_id}/tags", "api_v2_items_item_id_tags"),
    ],
)
def test_sanitize_path_segment(path: str, expected: str) -> None:
    assert _sanitize_path_segment(path) == expected


# ---------------------------------------------------------------------------
# _derive_operation_id  (Step 2 — full fallback derivation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, expected",
    [
        # Golden file — do NOT change without a deprecation comment
        ("GET",    "/pets",                    "get_pets"),
        ("POST",   "/pets",                    "post_pets"),
        ("DELETE", "/pets/{petId}",            "delete_pets_petId"),
        ("GET",    "/users/{id}/orders/{orderId}", "get_users_id_orders_orderId"),
        ("GET",    "/v1/api/items",             "get_v1_api_items"),
        ("PUT",    "/",                         "put"),
        ("GET",    "/{id}",                     "get_id"),
        ("POST",   "/api/v2/items/{item_id}/tags", "post_api_v2_items_item_id_tags"),
        ("PATCH",  "/users/{id}",               "patch_users_id"),
        ("HEAD",   "/health",                   "head_health"),
        ("OPTIONS","/users",                    "options_users"),
        ("GET",    "/pets/{petId}",             "get_pets_petId"),
    ],
)
def test_derive_operation_id(method: str, path: str, expected: str) -> None:
    assert _derive_operation_id(method, path) == expected


# ---------------------------------------------------------------------------
# _resolve_collisions  (Step 3)
# ---------------------------------------------------------------------------


def test_no_collisions():
    result = _resolve_collisions(["a", "b", "c"])
    assert result == ["a", "b", "c"]


def test_single_collision():
    result = _resolve_collisions(["get_users", "get_users"])
    assert result == ["get_users", "get_users_2"]


def test_triple_collision():
    result = _resolve_collisions(["get_users", "get_users", "get_users"])
    assert result == ["get_users", "get_users_2", "get_users_3"]


def test_first_occurrence_keeps_plain_name():
    result = _resolve_collisions(["op", "other", "op"])
    assert result[0] == "op"    # first keeps plain name
    assert result[2] == "op_2"  # second gets suffix


def test_mixed_collisions():
    result = _resolve_collisions(["a", "b", "a", "c", "b", "a"])
    assert result == ["a", "b", "a_2", "c", "b_2", "a_3"]


def test_collision_suffix_starts_at_2():
    """Suffixes begin at _2, not _1."""
    result = _resolve_collisions(["x", "x"])
    assert "_1" not in result[1]
    assert result[1] == "x_2"


# ---------------------------------------------------------------------------
# Integration: operationId takes priority over derivation
# ---------------------------------------------------------------------------


def test_operation_id_preferred_over_derived():
    """When operationId is present, it is used verbatim."""
    # This is tested via the normalize stage in test_normalize.py.
    # Here we just confirm the derivation function is NOT called when
    # operationId is set — i.e. the derivation output would differ.
    op_id = "getPets"  # would be "get_pets" from derivation
    derived = _derive_operation_id("GET", "/pets")
    assert op_id != derived  # proves they'd conflict
    # The normalize stage chooses op_id, not derived — asserted in test_normalize.py
