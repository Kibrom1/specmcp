"""specmcp — Convert any OpenAPI spec into a working MCP server."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__: str = version("specmcp")
except PackageNotFoundError:
    # Package is not installed (e.g. running directly from source tree)
    __version__ = "0.0.0+dev"

__mcp_sdk_version_constraint__ = ">=1.0.0,<2.0.0"
