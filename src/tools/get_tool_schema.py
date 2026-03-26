"""Schema retrieval tool."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.tools.search_tools import DEFAULT_CATALOG_PATH, load_catalog

logger = logging.getLogger(__name__)


def get_tool_schema(
    server: str,
    tool_name: str,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> dict[str, Any]:
    """
    Retrieve the full JSON Schema for a specific tool.

    Returns metadata + inputSchema, or an error dict if not found.
    """
    catalog = load_catalog(catalog_path)
    tool = catalog.find_tool(server, tool_name)

    if tool is None:
        return {
            "success": False,
            "error": f"Tool '{tool_name}' not found on server '{server}'",
        }

    backend = next((b for b in catalog.backends if b.id == server), None)
    updated_at = backend.last_cataloged_at.isoformat() if backend else None

    return {
        "success": True,
        "server": tool.server_id,
        "tool_name": tool.name,
        "key": tool.key,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "updated_at": updated_at,
    }
