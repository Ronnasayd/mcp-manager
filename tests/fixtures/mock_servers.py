"""Mock MCP servers for integration testing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

MOCK_TOOLS = {
    "server_a": [
        {
            "name": "search_records",
            "description": "Search database records",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
            },
        },
        {
            "name": "create_record",
            "description": "Create a new database record",
            "inputSchema": {
                "type": "object",
                "required": ["data"],
                "properties": {"data": {"type": "object"}},
            },
        },
    ],
    "server_b": [
        {
            "name": "list_files",
            "description": "List files in a directory",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
        {
            "name": "read_file",
            "description": "Read file contents",
            "inputSchema": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        },
        {
            "name": "write_file",
            "description": "Write content to a file",
            "inputSchema": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
    ],
}


def _make_response(req_id: Any, result: Any) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n"
    ).encode()


def _make_error(req_id: Any, message: str, code: int = -32603) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )
        + "\n"
    ).encode()


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server_id: str
) -> None:
    """Handle a single MCP client connection."""
    initialized = False
    tools = MOCK_TOOLS.get(server_id, [])
    tool_map = {t["name"]: t for t in tools}

    try:
        while True:
            line = await reader.readline()
            if not line:
                break

            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            req_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                writer.write(
                    _make_response(
                        req_id,
                        {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": server_id, "version": "1.0"},
                        },
                    )
                )
                await writer.drain()
                initialized = True

            elif method == "notifications/initialized":
                pass  # No response needed

            elif method == "tools/list":
                writer.write(_make_response(req_id, {"tools": tools}))
                await writer.drain()

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                if tool_name not in tool_map:
                    writer.write(_make_error(req_id, f"Tool '{tool_name}' not found"))
                    await writer.drain()
                else:
                    # Return a simple mock result
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock result from {server_id}/{tool_name}",
                            }
                        ],
                        "arguments_received": arguments,
                    }
                    writer.write(_make_response(req_id, result))
                    await writer.drain()

            elif req_id is not None:
                writer.write(_make_error(req_id, f"Method not found: {method}", -32601))
                await writer.drain()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


class MockMCPServer:
    """An in-process mock MCP TCP server for testing."""

    def __init__(self, server_id: str, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server_id = server_id
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self.actual_port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, self.server_id),
            host=self.host,
            port=self.port,
        )
        self.actual_port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def base_url(self) -> str:
        return f"http://{self.host}:{self.actual_port}"
