"""
Per-session state for the specmcp auth layer.

SessionContext is a lightweight carrier object created when an MCP session
opens and threaded through the dispatch pipeline. It tells the auth layer
which session is making the call so per-user tokens can be looked up.

What SessionContext does NOT hold:
  - The actual OAuth tokens. Those live in the TokenStore keyed by session_id.
  - Any request-specific state. It is created once per session, not per call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from specmcp.config import SensitiveStr


@dataclass
class SessionContext:
    """Carries per-session identity and optional client-supplied bearer token.

    Created once per MCP session (at initialize time) and reused for all
    tool calls within that session.

    Attributes:
        session_id: Random UUID assigned by specmcp at session open.
            Never exposed to the LLM — used only as a key into the token store.
        client_token: Optional bearer token passed by the MCP client in
            initialize.meta["bearer_token"]. When present, takes priority over
            any token in the token store. The MCP client is responsible for
            keeping this token fresh — specmcp does NOT manage refresh for
            client-supplied tokens.
        metadata: Arbitrary key-value data from the MCP initialize request.
            Stored for diagnostic purposes; not used by the auth layer.
    """

    session_id: str
    client_token: SensitiveStr | None = None
    metadata: dict = field(default_factory=dict)
