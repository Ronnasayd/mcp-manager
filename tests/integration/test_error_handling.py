"""Integration tests for error paths: timeouts, invalid inputs, backend failures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.call_tool import call_tool
from src.tools.get_tool_schema import get_tool_schema
from src.tools.search_tools import invalidate_catalog_cache, search_tools


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


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


@pytest.fixture
def catalog_path(tmp_path):
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
        assert result["success"] is False
        assert "Timeout" in result["error"]
        assert result["elapsed_ms"] >= 0


class TestValidationErrors:
    @pytest.mark.asyncio
    async def test_missing_required(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "my_tool", {}, manager, catalog_path=catalog_path
        )
        assert result["success"] is False
        assert "x" in result["error"]

    @pytest.mark.asyncio
    async def test_wrong_type(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "my_tool", {"x": 123}, manager, catalog_path=catalog_path
        )
        assert result["success"] is False
        assert "Validation failed" in result["error"]


class TestToolNotFound:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "srv", "ghost_tool", {"x": "y"}, manager, catalog_path=catalog_path
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_server(self, catalog_path):
        manager = MagicMock()
        result = await call_tool(
            "ghost_server", "my_tool", {"x": "y"}, manager, catalog_path=catalog_path
        )
        assert result["success"] is False

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
        assert result["success"] is False


class TestBackendCrash:
    @pytest.mark.asyncio
    async def test_generic_exception_wrapped(self, catalog_path):
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=Exception("process died"))
        result = await call_tool(
            "srv", "my_tool", {"x": "v"}, manager, catalog_path=catalog_path
        )
        assert result["success"] is False
        assert "process died" in result["error"]

    @pytest.mark.asyncio
    async def test_result_always_has_elapsed(self, catalog_path):
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=Exception("boom"))
        result = await call_tool(
            "srv", "my_tool", {"x": "v"}, manager, catalog_path=catalog_path
        )
        assert "elapsed_ms" in result
        assert isinstance(result["elapsed_ms"], float)
