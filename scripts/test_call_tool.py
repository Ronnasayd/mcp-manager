"""Test script: call tool and print the result."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.backends.connection_manager import ConnectionManager
from src.catalog.builder import resolve_config
from src.catalog.schema import BackendsConfig
from src.tools.call_tool import call_tool

ROOT = Path(__file__).resolve().parent.parent

BACKENDS_JSON = ROOT / "backends.json"
CATALOG_JSON = ROOT / "catalog.json"


async def main() -> None:
    """Call one backend tool through the proxy router and print the result."""
    # Load and register backends
    raw = json.loads(BACKENDS_JSON.read_text())
    config = BackendsConfig.model_validate(raw)
    config = resolve_config(config)

    manager = ConnectionManager()
    for server_id, cfg in config.servers.items():
        manager.register(server_id, cfg)

    server_id = "chrome-devtools"
    tool_name = "new_page"

    arguments = {
        "url": "https://www.google.com",
        # "projectRoot": str(ROOT),
        # "prompt": "create a pylint config file for this project",
    }

    print(f"Calling {server_id}/{tool_name} with arguments:")
    print(json.dumps(arguments, indent=2) + "\n")

    result = await call_tool(
        server=server_id,
        tool_name=tool_name,
        arguments=arguments,
        manager=manager,
        catalog_path=CATALOG_JSON,
    )

    print("Result:")
    print(json.dumps(result.model_dump(by_alias=True, exclude_none=True), indent=2))

    await manager.close_all()


if __name__ == "__main__":
    asyncio.run(main())
