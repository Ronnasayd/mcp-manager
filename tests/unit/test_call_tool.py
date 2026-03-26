"""Unit tests for call_tool argument validation and routing."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.call_tool import _validate_arguments, call_tool
from src.tools.search_tools import invalidate_catalog_cache


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


def _catalog_with_tool(input_schema: dict) -> Path:
    t = CatalogTool(
        server_id="srv",
        name="my_tool",
        description="test tool",
        input_schema=input_schema,
    )
    b = CatalogBackend(id="srv", name="srv", type="http", tools=[t])
    catalog = Catalog(backends=[b])
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    Path(f.name).write_text(catalog.model_dump_json())
    return Path(f.name)


class TestValidateArguments:
    def test_valid_required_field(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
        errors = _validate_arguments(schema, {"name": "hello"})
        assert errors == []

    def test_missing_required_field(self):
        schema = {"type": "object", "required": ["name"]}
        errors = _validate_arguments(schema, {})
        assert any("name" in e for e in errors)

    def test_wrong_type(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }
        errors = _validate_arguments(schema, {"count": "not_a_number"})
        assert any("count" in e for e in errors)

    def test_empty_schema_allows_anything(self):
        errors = _validate_arguments({}, {"anything": "goes"})
        assert errors == []

    def test_extra_fields_allowed(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        errors = _validate_arguments(schema, {"x": "hi", "extra": 123})
        assert errors == []


class TestCallTool:
    @pytest.mark.asyncio
    async def test_success(self):
        path = _catalog_with_tool(
            {
                "type": "object",
                "required": ["q"],
                "properties": {"q": {"type": "string"}},
            }
        )
        manager = MagicMock()
        manager.call_tool = AsyncMock(return_value={"data": "result"})

        result = await call_tool(
            "srv", "my_tool", {"q": "hello"}, manager, catalog_path=path
        )
        assert result["success"] is True
        assert result["result"] == {"data": "result"}
        assert result["error"] is None
        assert "elapsed_ms" in result

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        path = _catalog_with_tool({})
        manager = MagicMock()

        result = await call_tool(
            "srv", "nonexistent_tool", {}, manager, catalog_path=path
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_server_not_found(self):
        path = _catalog_with_tool({})
        manager = MagicMock()

        result = await call_tool("no_server", "my_tool", {}, manager, catalog_path=path)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_validation_failure(self):
        schema = {
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        }
        path = _catalog_with_tool(schema)
        manager = MagicMock()

        result = await call_tool(
            "srv", "my_tool", {"count": "not_int"}, manager, catalog_path=path
        )
        assert result["success"] is False
        assert "Validation failed" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_required_arg(self):
        schema = {"type": "object", "required": ["mandatory"]}
        path = _catalog_with_tool(schema)
        manager = MagicMock()

        result = await call_tool("srv", "my_tool", {}, manager, catalog_path=path)
        assert result["success"] is False
        assert "mandatory" in result["error"]

    @pytest.mark.asyncio
    async def test_backend_exception_wrapped(self):
        path = _catalog_with_tool({})
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=RuntimeError("backend exploded"))

        result = await call_tool("srv", "my_tool", {}, manager, catalog_path=path)
        assert result["success"] is False
        assert "backend exploded" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_wrapped(self):
        path = _catalog_with_tool({})
        manager = MagicMock()
        manager.call_tool = AsyncMock(side_effect=TimeoutError())

        result = await call_tool("srv", "my_tool", {}, manager, catalog_path=path)
        assert result["success"] is False
        assert "Timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_elapsed_ms_present(self):
        path = _catalog_with_tool({})
        manager = MagicMock()
        manager.call_tool = AsyncMock(return_value="ok")

        result = await call_tool("srv", "my_tool", {}, manager, catalog_path=path)
        assert isinstance(result["elapsed_ms"], float)
        assert result["elapsed_ms"] >= 0
