"""World Intelligence MCP Server — real-time global intelligence across 30+ domains."""

from importlib.metadata import PackageNotFoundError, version

from .diagnostics import suppress_transport_logs

# Applies to CLI, dashboard, MCP server and collector, regardless of which
# entrypoint later configures root logging or enables DEBUG diagnostics.
suppress_transport_logs()

try:
    __version__ = version("world-intel-mcp")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.0.0+uninstalled"
