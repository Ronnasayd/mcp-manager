"""In-memory MCP client tests for the call_tool passthrough."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, ImageContent, TextContent
import pytest

from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.proxy.server import build_mcp_server

if TYPE_CHECKING:
    from pathlib import Path

IMAGE = {"type": "image", "data": "aGk=", "mimeType": "image/png"}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the backend connection manager with a mock.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        AsyncMock: The mocked manager.call_tool.
    """
    manager = MagicMock()
    manager.call_tool = AsyncMock()
    monkeypatch.setattr("src.proxy.server._load_manager", lambda _path: manager)
    return manager.call_tool


async def _call(tmp_path: Path, tool_name: str = "my_tool") -> CallToolResult:
    """Call the proxy call_tool tool through an in-memory MCP client.

    Args:
        tmp_path: Directory for the temporary config and catalog.
        tool_name: Backend tool name to request.

    Returns:
        CallToolResult: The result as seen by the client.
    """
    tool = CatalogTool(server_id="srv", name="my_tool", description="d")
    catalog = Catalog(
        backends=[CatalogBackend(id="srv", name="srv", type="http", tools=[tool])]
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(catalog.model_dump_json(), encoding="utf-8")
    mcp, _ = build_mcp_server(tmp_path / "backends.json", catalog_path)

    async with create_connected_server_and_client_session(mcp) as client:
        return await client.call_tool(
            "call_tool", {"server": "srv", "tool_name": tool_name, "arguments": {}}
        )


class TestCallToolThroughMcpClient:
    async def test_json_text_is_single_encoded(self, tmp_path, backend):
        backend.return_value = {"content": [{"type": "text", "text": '{"a": 1}'}]}

        result = await _call(tmp_path)

        assert result.content == [TextContent(type="text", text='{"a": 1}')]

    async def test_image_block_arrives_as_image(self, tmp_path, backend):
        backend.return_value = {"content": [IMAGE]}

        result = await _call(tmp_path)

        assert result.content == [ImageContent(**IMAGE)]

    async def test_backend_error_visible(self, tmp_path, backend):
        backend.return_value = {
            "content": [{"type": "text", "text": "boom"}],
            "isError": True,
        }

        result = await _call(tmp_path)

        assert result.isError is True
        assert result.content == [TextContent(type="text", text="boom")]

    async def test_unknown_tool_is_error(self, tmp_path, backend):
        result = await _call(tmp_path, tool_name="ghost")

        assert result.isError is True
        assert result.content == [
            TextContent(type="text", text="Tool 'ghost' not found on server 'srv'")
        ]
        backend.assert_not_called()

    async def test_tool_description_has_no_envelope_fields(self, tmp_path, backend):
        mcp, _ = build_mcp_server(tmp_path / "backends.json", tmp_path / "c.json")

        async with create_connected_server_and_client_session(mcp) as client:
            tools = (await client.list_tools()).tools

        description = next(t.description for t in tools if t.name == "call_tool")
        assert description
        assert "elapsed_ms" not in description
        assert "success" not in description
