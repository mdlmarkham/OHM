"""OHM MCP Server — expose OHM knowledge graph as MCP tools for OpenClaw agents."""

# config.py is always importable (no mcp dependency)
from .config import config, load_config_file, is_tool_allowed, make_headers, WRITE_TOOLS

# server.py requires the mcp package — import lazily
try:
    from .server import mcp

    __all__ = ["mcp", "config", "load_config_file", "is_tool_allowed", "make_headers", "WRITE_TOOLS"]
except ImportError:
    __all__ = ["config", "load_config_file", "is_tool_allowed", "make_headers", "WRITE_TOOLS"]
