"""
specmcp Expose stage — Tool Registry.

Takes the list of SimplifiedOperations and builds an immutable ToolRegistry
that answers two questions:
  1. What tools should I expose? (tools/list)
  2. Given a tool name, which SimplifiedOperation handles it? (lookup)

Per-operation config overrides (rename, hide, redescribe) are applied here.

The registry is built once at startup and held immutably for the server's
lifetime. Hot-reload (P1) will replace it atomically by building a new
instance and swapping the reference — which is why this is a value type,
not a singleton.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from specmcp.config import Config, OperationOverride
from specmcp.core.model import SimplifiedOperation


@dataclass(frozen=True)
class ToolDefinition:
    """MCP tool definition as returned by tools/list."""

    name: str
    description: str
    input_schema: dict[str, Any]
    #: Back-reference to the source SimplifiedOperation (for dispatch).
    simplified_operation: SimplifiedOperation

    def to_mcp_dict(self) -> dict[str, Any]:
        """Serialise to the MCP tools/list wire format."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class ToolRegistry:
    """Immutable registry of all exposed MCP tools.

    Build with ``ToolRegistry.build()``. After construction, the registry
    is read-only — the server holds a single reference and never mutates it.
    """

    #: Ordered list of tools (MCP tools/list order = spec order).
    tools: list[ToolDefinition] = field(default_factory=list)
    #: Fast lookup: tool name → ToolDefinition. Built by __post_init__; not
    #: an __init__ parameter so callers cannot accidentally supply a stale index.
    _index: dict[str, ToolDefinition] = field(default_factory=dict, repr=False, init=False)

    def __post_init__(self) -> None:
        self._index = {t.name: t for t in self.tools}

    def lookup(self, name: str) -> ToolDefinition | None:
        """Return the ToolDefinition for *name*, or None if not found."""
        return self._index.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP tools/list payload."""
        return [t.to_mcp_dict() for t in self.tools]

    @classmethod
    def build(
        cls,
        simplified_ops: list[SimplifiedOperation],
        config: Config | None = None,
    ) -> "ToolRegistry":
        """Build the registry from simplified operations and optional config.

        Applies per-operation overrides (rename, hide, redescribe) from config.
        Operations marked ``hide: true`` are excluded.

        Parameters
        ----------
        simplified_ops:
            Output of the Simplify stage.
        config:
            Full specmcp config. If None, no overrides are applied.
        """
        tools: list[ToolDefinition] = []

        for sop in simplified_ops:
            op_id = sop.operation.id
            override: OperationOverride | None = None

            if config:
                override = config.operations.get(op_id)

            # Apply hide flag
            if override and override.hide:
                continue

            # Determine final tool name
            tool_name = (override.rename if override and override.rename else op_id)

            # Determine description
            if override and override.description:
                description = override.description
            else:
                description = sop.llm_description

            # Apply input schema strictness if requested
            input_schema = dict(sop.llm_input_schema)
            if override and override.additional_properties_strict:
                input_schema = {**input_schema, "additionalProperties": False}

            tools.append(ToolDefinition(
                name=tool_name,
                description=description,
                input_schema=input_schema,
                simplified_operation=sop,
            ))

        return cls(tools=tools)
