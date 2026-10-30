"""Agent integrations: the EEP tool registry, an MCP server and a built-in Claude agent."""

from .mcp_server import MCPServer
from .tools import Tool, ToolRegistry, build_tools, feedback_view

__all__ = ["MCPServer", "Tool", "ToolRegistry", "build_tools", "feedback_view"]
