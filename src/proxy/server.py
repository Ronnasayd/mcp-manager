"""Main FastMCP proxy server entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _load_manager(config_path: Path):
    from src.backends.connection_manager import ConnectionManager
    from src.catalog.builder import resolve_config
    from src.catalog.schema import BackendsConfig, BackendType

    raw = json.loads(config_path.read_text())
    config = BackendsConfig.model_validate(raw)
    config = resolve_config(config)

    manager = ConnectionManager()
    for server_id, cfg in config.servers.items():
        manager.register(server_id, cfg)
        logger.info('"Registered backend: %s (type=%s)"', server_id, cfg.type.value)

    return manager


def build_mcp_server(config_path: Path, catalog_path: Path):
    """Build and return a configured FastMCP server instance."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        from fastmcp import FastMCP

    manager = _load_manager(config_path)

    mcp = FastMCP(
        name="mcp-proxy",
        instructions=(
            "This is an MCP proxy server. Use search_tools to discover available tools, "
            "get_tool_schema to retrieve their input schemas, and call_tool to execute them. "
            "Use list_servers to see all available backends."
        ),
    )

    # -----------------------------------------------------------------------
    # Tool 1: search_tools
    # -----------------------------------------------------------------------
    @mcp.tool()
    def search_tools(query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """
        Search the tool catalog using fuzzy matching.

        Args:
            query: Search query string (tool name, description keywords, etc.)
            max_results: Maximum number of results to return (default: 10)

        Returns:
            List of matching tools with server, name, description, score, and key fields.
        """
        from src.tools.search_tools import search_tools as _search

        return _search(query, max_results=max_results, catalog_path=catalog_path)

    # -----------------------------------------------------------------------
    # Tool 2: get_tool_schema
    # -----------------------------------------------------------------------
    @mcp.tool()
    def get_tool_schema(server: str, tool_name: str) -> dict[str, Any]:
        """
        Retrieve the full JSON Schema for a specific tool.

        Args:
            server: Backend server ID (e.g., "github", "context7")
            tool_name: Name of the tool to retrieve schema for

        Returns:
            Tool metadata including description and inputSchema.
        """
        from src.tools.get_tool_schema import get_tool_schema as _get_schema

        return _get_schema(server, tool_name, catalog_path=catalog_path)

    # -----------------------------------------------------------------------
    # Tool 3: call_tool
    # -----------------------------------------------------------------------
    @mcp.tool()
    async def call_tool(
        server: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Execute a tool on a backend MCP server.

        Args:
            server: Backend server ID (e.g., "github", "context7")
            tool_name: Name of the tool to call
            arguments: Arguments to pass to the tool (must match its inputSchema)

        Returns:
            Result dict with success, result, error, and elapsed_ms fields.
        """
        from src.tools.call_tool import call_tool as _call

        return await _call(
            server, tool_name, arguments, manager, catalog_path=catalog_path
        )

    # -----------------------------------------------------------------------
    # Tool 4: list_servers
    # -----------------------------------------------------------------------
    @mcp.tool()
    def list_servers() -> list[dict[str, Any]]:
        """
        List all configured backend servers and their status.

        Returns:
            List of server info with id, name, type, status, tool_count, and last_cataloged_at.
        """
        from src.tools.list_servers import list_servers as _list

        return _list(manager, catalog_path=catalog_path)

    return mcp, manager


def main() -> None:
    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(description="MCP Proxy Server")
    parser.add_argument(
        "--config",
        default=os.environ.get("MCP_PROXY_CONFIG", "src/config/backends.json"),
        help="Path to backends.json",
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("MCP_PROXY_CATALOG", "catalog.json"),
        help="Path to catalog.json",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("MCP_PROXY_TRANSPORT", "stdio"),
        choices=["stdio", "sse"],
        help="Transport mode",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PROXY_PORT", "8000")),
        help="Port for HTTP/SSE mode",
    )
    parser.add_argument(
        "--build-catalog",
        action="store_true",
        help="Build catalog before starting (runs catalog builder)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    catalog_path = Path(args.catalog)

    if not config_path.exists():
        logger.error('"Config file not found: %s"', config_path)
        sys.exit(1)

    if args.build_catalog:
        from src.catalog.builder import build_catalog

        asyncio.run(build_catalog(config_path, catalog_path))

    mcp, manager = build_mcp_server(config_path, catalog_path)

    async def _run():
        try:
            await manager.start_cleanup_loop()
            if args.transport == "stdio":
                await mcp.run_async(transport="stdio")
            else:
                await mcp.run_async(transport="sse", port=args.port)
        finally:
            await manager.close_all()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
