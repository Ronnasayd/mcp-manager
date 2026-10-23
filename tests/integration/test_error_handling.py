"""Integration tests for error paths: timeouts, invalid inputs, backend failures."""

# why: pytest groups related cases in classes that hold a single test.
# pylint: disable=too-few-public-methods

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from mcp.types import CallToolResult, TextContent
import pytest

from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.call_tool import call_tool
from src.tools.get_tool_schema import get_tool_schema
from src.tools.search_tools import search_tools

if TYPE_CHECKING:
    from pathlib import Path


def _simple_catalog(path: Path) -> None:
    t = CatalogTool(
        server_id="srv",
        name="my_tool",
        description="A tool",
        input_schema={
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "string"}},
        },
    )
    catalog = Catalog(
        backends=[CatalogBackend(id="srv", name="srv", type="http", tools=[t])]
    )
    path.write_text(catalog.model_dump_json())


def _error_text(result: CallToolResult) -> str:
    """Assert result is a proxy error and return its single text.

    Args:
        result: Proxy result.

    Returns:
        str: Text of the single error block.
    """
    assert result.isError is True
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    """Write the single-tool catalog under tmp_path.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path: Path to the written catalog.
    """
    p = tmp_path / "catalog.json"
    _simple_catalog(p)
    return p


class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, catalog_path):
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=TimeoutError())

        result = await call_tool(
            "srv", "my_tool", {"x": "hello"}, manager, catalog_path=catalog_path
        )
        assert _error_text(result) == "Timeout calling 'my_tool' on server 'srv'"


class TestValidationErrors:
    @pytest.mark.asyncio
    async def test_missing_required(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "my_tool", {}, manager, catalog_path=catalog_path
        )
        assert _error_text(result).startswith(
            "Validation failed: Missing required argument: 'x'\n\nExpected schema:\n"
        )

    @pytest.mark.asyncio
    async def test_wrong_type(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "my_tool", {"x": 123}, manager, catalog_path=catalog_path
        )
        assert _error_text(result).startswith(
            "Validation failed: Argument 'x': expected type string, got int\n\n"
        )


class TestToolNotFound:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "ghost_tool", {"x": "y"}, manager, catalog_path=catalog_path
        )
        assert _error_text(result) == "Tool 'ghost_tool' not found on server 'srv'"

    @pytest.mark.asyncio
    async def test_unknown_server(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "ghost_server", "my_tool", {"x": "y"}, manager, catalog_path=catalog_path
        )
        assert _error_text(result) == (
            "Tool 'my_tool' not found on server 'ghost_server'"
        )

    def test_get_schema_unknown_server(self, catalog_path):
        result = get_tool_schema("ghost_server", "my_tool", catalog_path=catalog_path)
        assert result["success"] is False
        assert "not found" in result["error"]


class TestEmptyState:
    def test_search_empty_catalog(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text(Catalog().model_dump_json())
        results = search_tools("anything", catalog_path=p)
        assert results == []

    @pytest.mark.asyncio
    async def test_call_empty_catalog(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text(Catalog().model_dump_json())
        manager = MagicMock()
        result = await call_tool("srv", "tool", {}, manager, catalog_path=p)
        assert _error_text(result) == "Tool 'tool' not found on server 'srv'"


class TestBackendCrash:
    @pytest.mark.asyncio
    async def test_generic_exception_wrapped(self, catalog_path):
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=Exception("process died"))
        result = await call_tool(
            "srv", "my_tool", {"x": "v"}, manager, catalog_path=catalog_path
        )
        assert _error_text(result) == "process died"

    @pytest.mark.asyncio
    async def test_crash_logs_elapsed_once(self, catalog_path, caplog):
        caplog.set_level(logging.INFO, logger="src.tools.call_tool")
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=Exception("boom"))
        await call_tool(
            "srv", "my_tool", {"x": "v"}, manager, catalog_path=catalog_path
        )
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        assert "elapsed_ms=" in infos[0].getMessage()
