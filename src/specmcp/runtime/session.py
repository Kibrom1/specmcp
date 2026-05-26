"""Per-session state for the specmcp auth layer."""

from __future__ import annotations

from dataclasses import dataclass, field

from specmcp.config import SensitiveStr


@dataclass
class SessionContext:
    """Carries per-session identity and optional client-supplied bearer token.

    Created once per MCP session and reused for all tool calls within it.

    Attributes:
        session_id: Random UUID assigned by specmcp at session open.
            Never exposed to the LLM — used only as a key into the token store.
        client_token: Optional bearer token passed by the MCP client in
            initialize._meta.bearer_token. Takes priority over token store lookup.
            The MCP client is responsible for keeping this token fresh.
        metadata: Arbitrary key-value data from the MCP initialize request.
    """

    session_id: str
    client_token: SensitiveStr | None = None
    metadata: dict = field(default_factory=dict)
