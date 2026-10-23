"""Unit tests for call_tool argument validation and routing."""

# why: pytest groups related cases in classes that hold a single test.
# pylint: disable=too-few-public-methods

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from mcp.types import CallToolResult, ImageContent, TextContent
import pytest

from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.call_tool import _validate_arguments, call_tool
from src.tools.search_tools import invalidate_catalog_cache

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def reset_cache() -> Iterator[None]:
    """Clear the global catalog cache around each test.

    Yields:
        None: Control to the test.
    """
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
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    return path


class TestValidateArguments:
    def test_valid_required_field(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
        errors = _validate_arguments(schema, {"name": "hello"})
        assert not errors

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
        assert not errors

    def test_extra_fields_allowed(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        errors = _validate_arguments(schema, {"x": "hi", "extra": 123})
        assert not errors


SCHEMA_Q = {
    "type": "object",
    "required": ["q"],
    "properties": {"q": {"type": "string"}},
}
LOGGER = "src.tools.call_tool"


async def _run(backend_result: object, schema: dict | None = None) -> CallToolResult:
    """Run call_tool with a manager whose backend returns backend_result.

    Args:
        backend_result: Raw value the mocked backend returns.
        schema: Input schema registered for srv/my_tool.

    Returns:
        CallToolResult: The proxy result.
    """
    path = _catalog_with_tool(schema or {})
    manager = MagicMock()
    manager.call_tool = AsyncMock(return_value=backend_result)
    return await call_tool("srv", "my_tool", {}, manager, catalog_path=path)


async def _run_raising(exc: BaseException) -> CallToolResult:
    """Run call_tool with a manager whose backend raises exc.

    Args:
        exc: Exception raised by the mocked backend.

    Returns:
        CallToolResult: The proxy result.
    """
    path = _catalog_with_tool({})
    manager = MagicMock()
    manager.call_tool = AsyncMock(side_effect=exc)
    return await call_tool("srv", "my_tool", {}, manager, catalog_path=path)


def _only_text(result: CallToolResult) -> str:
    """Return the text of the single text block in result.

    Args:
        result: Proxy result expected to hold exactly one text block.

    Returns:
        str: The text of that block.
    """
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _info_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return INFO records emitted by the call_tool module.

    Args:
        caplog: Pytest log capture fixture.

    Returns:
        list[logging.LogRecord]: Matching records.
    """
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


class TestCallTool:
    async def test_success(self):
        path = _catalog_with_tool(SCHEMA_Q)
        manager = MagicMock()
        manager.call_tool = AsyncMock(
            return_value={"content": [{"type": "text", "text": "hello"}]}
        )

        result = await call_tool(
            "srv", "my_tool", {"q": "hello"}, manager, catalog_path=path
        )

        assert isinstance(result, CallToolResult)
        assert result.content == [TextContent(type="text", text="hello")]
        assert result.isError is False

    async def test_json_text_kept_verbatim(self):
        result = await _run({"content": [{"type": "text", "text": '{"a": 1}'}]})

        assert _only_text(result) == '{"a": 1}'

    async def test_image_block_passthrough(self):
        image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}

        result = await _run({"content": [image]})

        assert result.content == [ImageContent(**image)]

    async def test_structured_content_passthrough(self):
        result = await _run({"content": [], "structuredContent": {"answer": 42}})

        assert result.structuredContent == {"answer": 42}

    async def test_backend_is_error_passthrough(self):
        result = await _run(
            {"content": [{"type": "text", "text": "bad input"}], "isError": True}
        )

        assert result.isError is True
        assert _only_text(result) == "bad input"

    async def test_meta_passthrough(self):
        result = await _run({"content": [], "_meta": {"trace": "abc"}})

        assert result.meta == {"trace": "abc"}

    async def test_empty_content_without_is_error(self):
        result = await _run({"content": []})

        assert result.content == []
        assert result.isError is False

    async def test_null_top_level_keys_dropped(self):
        result = await _run(
            {"content": [{"type": "text", "text": "x"}], "isError": None}
        )

        assert result.isError is False
        assert _only_text(result) == "x"

    @pytest.mark.parametrize(
        "raw",
        [
            {"content": "x"},
            [{"type": "text", "text": "x"}],
            {"content": [{"type": "weird"}]},
        ],
    )
    async def test_off_spec_result_falls_back_to_json_text(self, raw, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)

        result = await _run(raw)

        assert result.isError is False
        assert _only_text(result) == json.dumps(raw)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "srv" in warnings[0].getMessage()
        assert "my_tool" in warnings[0].getMessage()

    async def test_tool_not_found(self):
        path = _catalog_with_tool({})
        manager = MagicMock()

        result = await call_tool(
            "srv", "nonexistent_tool", {}, manager, catalog_path=path
        )

        assert result.isError is True
        assert _only_text(result) == "Tool 'nonexistent_tool' not found on server 'srv'"

    async def test_server_not_found(self):
        result = await _run_raising(KeyError("Unknown server: srv"))

        assert result.isError is True
        assert _only_text(result) == "Unknown server: srv"

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

        assert result.isError is True
        assert _only_text(result) == (
            "Validation failed: Argument 'count': expected type integer, got str"
            "\n\nExpected schema:\n" + json.dumps(schema, indent=2)
        )

    async def test_missing_required_arg(self):
        schema = {"type": "object", "required": ["mandatory"]}
        path = _catalog_with_tool(schema)
        manager = MagicMock()

        result = await call_tool("srv", "my_tool", {}, manager, catalog_path=path)

        assert result.isError is True
        assert _only_text(result) == (
            "Validation failed: Missing required argument: 'mandatory'"
            "\n\nExpected schema:\n" + json.dumps(schema, indent=2)
        )

    async def test_backend_exception_wrapped(self, caplog):
        caplog.set_level(logging.ERROR, logger=LOGGER)

        result = await _run_raising(RuntimeError("backend exploded"))

        assert result.isError is True
        assert _only_text(result) == "backend exploded"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None

    async def test_timeout_wrapped(self):
        result = await _run_raising(TimeoutError())

        assert result.isError is True
        assert _only_text(result) == "Timeout calling 'my_tool' on server 'srv'"

    async def test_success_logs_elapsed_once(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)

        await _run({"content": []})

        records = _info_records(caplog)
        assert len(records) == 1
        assert re.search(r"srv.*my_tool.*elapsed_ms=\d+\.\d+", records[0].getMessage())

    async def test_error_branch_logs_elapsed_once(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        path = _catalog_with_tool({})

        await call_tool("srv", "ghost", {}, MagicMock(), catalog_path=path)

        records = _info_records(caplog)
        assert len(records) == 1
        assert re.search(r"srv.*ghost.*elapsed_ms=\d+\.\d+", records[0].getMessage())
