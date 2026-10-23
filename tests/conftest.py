"""Shared pytest fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from src.tools.search_tools import invalidate_catalog_cache

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def reset_cache() -> Iterator[None]:
    """Clear the global catalog and vector caches around each test.

    Yields:
        None: Control to the test.
    """
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()
