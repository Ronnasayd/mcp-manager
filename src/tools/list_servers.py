"""Backend server status enumeration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.backends.connection_manager import ConnectionManager
from src.tools.search_tools import DEFAULT_CATALOG_PATH, load_catalog

logger = logging.getLogger(__name__)


def list_servers(
    manager: ConnectionManager,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> list[dict[str, Any]]:
    """
    Enumerate all configured backends with their current status.

    Returns:
        [
            {
                "id": str,
                "name": str,
                "type": str,
                "status": "ready" | "initializing" | "error",
                "tool_count": int,
                "last_cataloged_at": str | None,
                "error": str | None,
            },
            ...
        ]
    """
    catalog = load_catalog(catalog_path)
    catalog_map = {b.id: b for b in catalog.backends}

    result = []
    for server_id in manager.server_ids():
        backend_entry = catalog_map.get(server_id)
        is_alive = manager.is_alive(server_id)

        if backend_entry and backend_entry.error:
            status = "error"
        elif is_alive:
            status = "ready"
        else:
            status = "initializing"

        result.append(
            {
                "id": server_id,
                "name": backend_entry.name if backend_entry else server_id,
                "type": backend_entry.type if backend_entry else "unknown",
                "status": status,
                "tool_count": backend_entry.tool_count if backend_entry else 0,
                "last_cataloged_at": (
                    backend_entry.last_cataloged_at.isoformat()
                    if backend_entry
                    else None
                ),
                "error": backend_entry.error if backend_entry else None,
            }
        )

    return result
