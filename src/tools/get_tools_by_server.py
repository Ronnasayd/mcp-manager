"""Get tools available for a specific server."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.tools.search_tools import DEFAULT_CATALOG_PATH, load_catalog

logger = logging.getLogger(__name__)


def get_tools_by_server(
    server_id: str,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> list[dict[str, Any]]:
    """
    Get all tools available for a specific server.

    Args:
        server_id: Backend server ID (e.g., "github", "context7")
        catalog_path: Path to the catalog file

    Returns:
        List of tools with name and description for the server.
        Returns empty list if server is not found.

        [
            {
                "name": str,
                "description": str,
            },
            ...
        ]
    """
    try:
        catalog = load_catalog(catalog_path)
    except Exception as e:
        logger.error(f"Failed to load catalog: {e}")
        return []

    # Find the backend with matching id
    for backend in catalog.backends:
        if backend.id == server_id:
            # Extract name and description from each tool
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                }
                for tool in backend.tools
            ]

    # Server not found
    logger.debug(f"Server '{server_id}' not found in catalog")
    return []
