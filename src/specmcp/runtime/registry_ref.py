"""
specmcp RegistryRef — mutable reference to the active ToolRegistry.

Used by --watch mode to atomically swap the registry when a spec or config
file changes, without dropping the stdio connection or interrupting in-flight
tool calls.

Design notes:
  - get() is lock-free: asyncio's single-threaded event loop makes a plain
    attribute read atomic — no coroutine can observe a half-written reference.
  - swap() acquires the lock so that two concurrent reloads (unlikely but
    possible with rapid file saves) serialise and the last one wins.
  - The lock is intentionally NOT held during get() so that a slow reload
    does not queue up every concurrent tools/list and tools/call call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from specmcp.core.expose import ToolRegistry


@dataclass
class RegistryRef:
    """Mutable, asyncio-safe reference to the current ToolRegistry.

    Usage::

        ref = RegistryRef(initial_registry)

        # In handlers — lock-free:
        reg = ref.get()
        tool = reg.lookup(name)

        # In the watcher — serialised:
        await ref.swap(new_registry)
    """

    _registry: ToolRegistry
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def get(self) -> ToolRegistry:
        """Return the current registry.

        Lock-free and safe to call from any coroutine. In asyncio's
        cooperative concurrency model, attribute reads are never torn.
        """
        return self._registry

    async def swap(self, new_registry: ToolRegistry) -> None:
        """Atomically replace the registry.

        Acquires the lock to serialise concurrent swap calls (e.g. two rapid
        saves triggering two reloads). Callers that call get() concurrently
        are unaffected — they see either the old or the new registry, never
        a partial write.
        """
        async with self._lock:
            self._registry = new_registry
