"""Test script: call tool and print the result."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BACKENDS_JSON = ROOT / "backends.json"
CATALOG_JSON = ROOT / "catalog.json"


async def main() -> None:
    from src.backends.connection_manager import ConnectionManager
    from src.catalog.builder import resolve_config
    from src.catalog.schema import BackendsConfig
    from src.tools.call_tool import call_tool

    # Load and register backends
    raw = json.loads(BACKENDS_JSON.read_text())
    config = BackendsConfig.model_validate(raw)
    config = resolve_config(config)

    manager = ConnectionManager()
    for server_id, cfg in config.servers.items():
        manager.register(server_id, cfg)

    # Arguments for add_task
    arguments = {
        "projectRoot": str(ROOT),
        "prompt": "create a pylint config file for this project",
    }

    print(
        f"Calling taskmaster-ai/add_task with arguments:\n{json.dumps(arguments, indent=2)}\n"
    )

    result = await call_tool(
        server="taskmaster-ai",
        tool_name="add_task",
        arguments=arguments,
        manager=manager,
        catalog_path=CATALOG_JSON,
    )

    print("Result:")
    print(json.dumps(result, indent=2, default=str))

    await manager.close_all()


if __name__ == "__main__":
    asyncio.run(main())
