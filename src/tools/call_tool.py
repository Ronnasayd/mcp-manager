"""Tool execution router with argument validation and error handling."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from src.tools.search_tools import DEFAULT_CATALOG_PATH, load_catalog

if TYPE_CHECKING:
    from pathlib import Path

    from src.backends.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


def _validate_arguments(input_schema: dict, arguments: dict) -> list[str]:
    """Minimal JSON Schema validation: check required fields and basic types.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    if not input_schema:
        return errors

    required = input_schema.get("required", [])
    errors.extend(
        f"Missing required argument: '{field}'"
        for field in required
        if field not in arguments
    )

    properties = input_schema.get("properties", {})
    type_map: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
    }

    for key, value in arguments.items():
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        expected_type_str = prop_schema.get("type")
        if not expected_type_str:
            continue
        allowed_type_strs = (
            expected_type_str
            if isinstance(expected_type_str, list)
            else [expected_type_str]
        )
        expected_py_types_tuple: tuple[type, ...] = tuple(
            py_type
            for allowed_type_str in allowed_type_strs
            for py_type in type_map.get(allowed_type_str, ())
        )
        if expected_py_types_tuple and not isinstance(value, expected_py_types_tuple):
            errors.append(
                f"Argument '{key}': expected type {expected_type_str}, "
                f"got {type(value).__name__}"
            )

    return errors


def _error_result(message: str) -> CallToolResult:
    """Build a proxy-side error result.

    Args:
        message: Error text shown to the client.

    Returns:
        CallToolResult: isError result with a single text block.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=message)], isError=True
    )


def _to_call_tool_result(raw: object, server: str, tool_name: str) -> CallToolResult:
    """Convert a raw backend result into a CallToolResult.

    Args:
        raw: Raw JSON-RPC result returned by the backend.
        server: Backend server name, for logging.
        tool_name: Tool name, for logging.

    Returns:
        CallToolResult: The validated backend result, or the raw JSON as text
        when the backend result is off-spec.
    """
    if isinstance(raw, dict):
        cleaned = {k: v for k, v in raw.items() if v is not None}
        try:
            return CallToolResult.model_validate(cleaned)
        except ValidationError:
            pass
    logger.warning(
        "[%s/%s] Backend result is not a valid CallToolResult", server, tool_name
    )
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(raw))])


async def call_tool(
    server: str,
    tool_name: str,
    arguments: dict[str, Any],
    manager: ConnectionManager,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> CallToolResult:
    """Execute a tool on a backend server.

    Args:
        server: Backend server name.
        tool_name: Tool name on that server.
        arguments: Arguments to pass to the tool.
        manager: Connection manager used to reach the backend.
        catalog_path: Path to the tool catalog.

    Returns:
        CallToolResult: The backend result passed through unchanged, or an
        isError result describing a proxy-side failure.
    """
    start = time.monotonic()
    try:
        tool = load_catalog(catalog_path).find_tool(server, tool_name)
        if tool is None:
            return _error_result(f"Tool '{tool_name}' not found on server '{server}'")

        validation_errors = _validate_arguments(tool.input_schema, arguments)
        if validation_errors:
            return _error_result(
                "Validation failed: "
                + "; ".join(validation_errors)
                + "\n\nExpected schema:\n"
                + json.dumps(tool.input_schema, indent=2)
            )

        try:
            raw = await manager.call_tool(server, tool_name, arguments)
        except TimeoutError:
            return _error_result(f"Timeout calling '{tool_name}' on server '{server}'")
        except KeyError as e:
            return _error_result(str(e.args[0]) if e.args else str(e))
        # why: any backend failure must reach the client as an isError result.
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("[%s/%s] Tool call failed", server, tool_name)
            return _error_result(str(e))
        return _to_call_tool_result(raw, server, tool_name)
    finally:
        logger.info(
            "[%s/%s] call_tool elapsed_ms=%.2f",
            server,
            tool_name,
            (time.monotonic() - start) * 1000,
        )
