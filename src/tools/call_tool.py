"""Tool execution router with argument validation and error handling."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.backends.connection_manager import ConnectionManager
from src.tools.search_tools import DEFAULT_CATALOG_PATH, load_catalog

logger = logging.getLogger(__name__)


def _validate_arguments(input_schema: dict, arguments: dict) -> list[str]:
    """
    Minimal JSON Schema validation: check required fields and basic types.
    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    if not input_schema:
        return errors

    required = input_schema.get("required", [])
    for field in required:
        if field not in arguments:
            errors.append(f"Missing required argument: '{field}'")

    properties = input_schema.get("properties", {})
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    for key, value in arguments.items():
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        expected_type_str = prop_schema.get("type")
        if not expected_type_str:
            continue
        expected_py_type = type_map.get(expected_type_str)
        if expected_py_type and not isinstance(value, expected_py_type):
            errors.append(
                f"Argument '{key}': expected type {expected_type_str}, "
                f"got {type(value).__name__}"
            )

    return errors


async def call_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any],
    manager: ConnectionManager,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> dict[str, Any]:
    """
    Execute a tool on a backend server.

    Returns:
        {
            "success": bool,
            "result": any | None,
            "error": str | None,
            "elapsed_ms": float,
        }
    """
    start = time.monotonic()

    # Validate tool exists in catalog
    catalog = load_catalog(catalog_path)
    tool = catalog.find_tool(server, tool_name)

    if tool is None:
        return {
            "success": False,
            "result": None,
            "error": f"Tool '{tool_name}' not found on server '{server}'",
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }

    # Validate arguments against schema
    validation_errors = _validate_arguments(tool.input_schema, arguments)
    if validation_errors:
        return {
            "success": False,
            "result": None,
            "error": "Validation failed: " + "; ".join(validation_errors),
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }

    # Execute
    try:
        result = await manager.call_tool(server, tool_name, arguments)
        return {
            "success": True,
            "result": result,
            "error": None,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }
    except TimeoutError:
        return {
            "success": False,
            "result": None,
            "error": f"Timeout calling '{tool_name}' on server '{server}'",
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }
    except KeyError as e:
        return {
            "success": False,
            "result": None,
            "error": str(e),
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }
    except Exception as e:
        logger.exception("[%s/%s] Tool call failed", server, tool_name)
        return {
            "success": False,
            "result": None,
            "error": str(e),
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }
