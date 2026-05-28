from abc import ABC, abstractmethod
import argparse

from mcp.server.fastmcp import FastMCP


class LifecycleConfigAbstract(ABC):
    """MCP server lifecycle configuration abstract class."""

    @abstractmethod
    def __init__(self, args: argparse.Namespace) -> None:
        """Initialize the MCP tool.

        This sets the global variables that tool calls will need.

        Called from main.py:initialize()
        """
        pass

    @staticmethod
    @abstractmethod
    def add_tools(mcp: FastMCP) -> None:
        """Add the module's MCP tools to the server."""
        # mcp.add_tool(method, name="toolname", title="short description")
        pass
