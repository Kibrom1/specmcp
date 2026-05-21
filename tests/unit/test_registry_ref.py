"""
Unit tests for specmcp.runtime.registry_ref.RegistryRef.

Tests:
  - get() returns the initial registry
  - swap() replaces the registry
  - get() after swap() returns the new registry
  - concurrent reads during a swap see a consistent value (either old or new)
  - concurrent swaps serialise — last writer wins
"""

from __future__ import annotations

import asyncio

import pytest

from specmcp.runtime.registry_ref import RegistryRef


# ---------------------------------------------------------------------------
# Minimal ToolRegistry stub (avoids importing the full pipeline)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal stand-in for ToolRegistry with an identity label."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.tools: list = []

    def __repr__(self) -> str:
        return f"FakeRegistry({self.label!r})"


# ---------------------------------------------------------------------------
# Basic get / swap
# ---------------------------------------------------------------------------


def test_get_returns_initial_registry() -> None:
    reg = _FakeRegistry("v1")
    ref = RegistryRef(reg)  # type: ignore[arg-type]
    assert ref.get() is reg


@pytest.mark.asyncio
async def test_swap_replaces_registry() -> None:
    reg_v1 = _FakeRegistry("v1")
    reg_v2 = _FakeRegistry("v2")
    ref = RegistryRef(reg_v1)  # type: ignore[arg-type]

    await ref.swap(reg_v2)  # type: ignore[arg-type]

    assert ref.get() is reg_v2
    assert ref.get().label == "v2"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_after_multiple_swaps() -> None:
    ref = RegistryRef(_FakeRegistry("v1"))  # type: ignore[arg-type]
    for i in range(2, 6):
        await ref.swap(_FakeRegistry(f"v{i}"))  # type: ignore[arg-type]
    assert ref.get().label == "v5"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Concurrency: reads during a swap see a consistent value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_reads_see_consistent_value() -> None:
    """Concurrent get() calls during a swap must each see either the old
    or the new registry — never a partially-written reference.

    In asyncio cooperative concurrency this is guaranteed because Python
    attribute assignment is atomic at the interpreter level and there is no
    preemption between awaits.  This test documents that contract.
    """
    import anyio

    reg_v1 = _FakeRegistry("v1")
    reg_v2 = _FakeRegistry("v2")
    ref = RegistryRef(reg_v1)  # type: ignore[arg-type]

    observed: list[str] = []

    async def reader() -> None:
        # Yield control, then read — may run before or after the swap.
        await asyncio.sleep(0)
        observed.append(ref.get().label)  # type: ignore[attr-defined]

    async def writer() -> None:
        await ref.swap(reg_v2)  # type: ignore[arg-type]

    # Launch 10 readers and 1 writer concurrently using anyio (Python 3.10 compat).
    async with anyio.create_task_group() as tg:
        for _ in range(10):
            tg.start_soon(reader)
        tg.start_soon(writer)

    # Every observed value must be either "v1" or "v2" — never something else.
    assert all(label in ("v1", "v2") for label in observed), observed
    # After all tasks complete, the registry must be v2.
    assert ref.get() is reg_v2


# ---------------------------------------------------------------------------
# Concurrency: concurrent swaps serialise — last writer wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_swaps_serialise() -> None:
    """Two concurrent swap() calls must not corrupt the reference.
    The last swap to acquire the lock wins; the result is one of the two
    new registries, never the initial one or a torn value.
    """
    import anyio

    ref = RegistryRef(_FakeRegistry("initial"))  # type: ignore[arg-type]
    reg_a = _FakeRegistry("A")
    reg_b = _FakeRegistry("B")

    async def swap_a() -> None:
        await ref.swap(reg_a)  # type: ignore[arg-type]

    async def swap_b() -> None:
        await ref.swap(reg_b)  # type: ignore[arg-type]

    async with anyio.create_task_group() as tg:
        tg.start_soon(swap_a)
        tg.start_soon(swap_b)

    final_label = ref.get().label  # type: ignore[attr-defined]
    assert final_label in ("A", "B"), f"Unexpected final label: {final_label!r}"


# ---------------------------------------------------------------------------
# get() is synchronous (no await required)
# ---------------------------------------------------------------------------


def test_get_is_synchronous() -> None:
    """get() must be a plain def, not a coroutine, so handlers can call it
    without await and without touching the event loop."""
    import inspect

    ref = RegistryRef(_FakeRegistry("sync-check"))  # type: ignore[arg-type]
    result = ref.get()
    # If get() returned a coroutine, `result` would be a coroutine object,
    # not a _FakeRegistry — this assertion would catch that.
    assert not inspect.iscoroutine(result)
    assert result.label == "sync-check"  # type: ignore[attr-defined]
