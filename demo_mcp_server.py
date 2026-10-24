"""Tiny demo MCP server. Run by aios.py over stdio. Requires: pip install mcp"""
try:
    from mcp.server.mcpserver import MCPServer  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x

mcp = MCPServer("demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


if __name__ == "__main__":
    mcp.run()
