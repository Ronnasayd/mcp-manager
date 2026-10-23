"""Test script: call tool and print the result."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.proxy.server import build_mcp_server
from src.tools.call_tool import call_tool

ROOT = Path(__file__).resolve().parent.parent

BACKENDS_JSON = ROOT / "backends.json"
CATALOG_JSON = ROOT / "catalog.json"


async def main() -> None:
    """Call one backend tool through the proxy router and print the result."""
    _, manager = build_mcp_server(BACKENDS_JSON, CATALOG_JSON)

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
