"""Fuzzy search over the tool catalog."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from src.catalog.schema import Catalog, CatalogTool

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path("catalog.json")
_catalog_cache: Catalog | None = None


def load_catalog(catalog_path: Path = DEFAULT_CATALOG_PATH) -> Catalog:
    """Load (and cache) the catalog from disk."""
    global _catalog_cache
    if _catalog_cache is None:
        if not catalog_path.exists():
            logger.warning(
                "Catalog not found at %s — returning empty catalog", catalog_path
            )
            return Catalog()
        _catalog_cache = Catalog.model_validate_json(catalog_path.read_text())
    return _catalog_cache


def invalidate_catalog_cache() -> None:
    global _catalog_cache
    _catalog_cache = None


def _score_tool(tool: CatalogTool, query: str) -> float:
    """Compute a combined fuzzy relevance score for a tool against a query."""
    name_score = fuzz.token_set_ratio(query, tool.name)
    desc_score = (
        fuzz.token_set_ratio(query, tool.description) if tool.description else 0
    )
    # Weighted: name matters more than description
    return 0.7 * name_score + 0.3 * desc_score


def search_tools(
    query: str,
    max_results: int = 10,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> list[dict[str, Any]]:
    """
    Search the tool catalog using fuzzy matching.

    Returns a list of dicts with keys: server, name, description, score, key.
    Results are sorted by descending score, limited to max_results.
    """
    catalog = load_catalog(catalog_path)
    tools = catalog.all_tools()

    if not tools:
        return []

    scored: list[tuple[float, CatalogTool]] = []
    for tool in tools:
        score = _score_tool(tool, query)
        scored.append((score, tool))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_results]

    return [
        {
            "server": t.server_id,
            "name": t.name,
            "key": t.key,
            "description": t.description,
            "score": round(score, 2),
        }
        for score, t in top
        if score > 0
    ]
