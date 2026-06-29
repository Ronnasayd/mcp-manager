"""Vector similarity search over the tool catalog."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.catalog.schema import Catalog
from src.catalog.vector_store import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path("catalog.json")
_catalog_cache: Catalog | None = None
_vector_store_cache: VectorStore | None = None


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


def load_vector_store(db_path: Path) -> VectorStore | None:
    """Load (and cache) the vector store from *db_path*.

    Returns None if the db file is missing, logging a warning.
    """
    global _vector_store_cache
    if _vector_store_cache is None:
        if not db_path.exists():
            logger.warning(
                "Vector index not found at %s — run catalog-builder first", db_path
            )
            return None
        _vector_store_cache = VectorStore.load(db_path)
    return _vector_store_cache


def invalidate_catalog_cache() -> None:
    global _catalog_cache, _vector_store_cache
    _catalog_cache = None
    _vector_store_cache = None


def search_tools(
    query: str,
    max_results: int = 10,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> list[dict[str, Any]]:
    """Search the tool catalog using vector cosine similarity.

    Returns a list of dicts with keys: server, name, description, score, key.
    Results sorted by descending score, limited to max_results.
    Score is in 0-100 range (cosine_sim * 100).

    Example: search_tools("retrieve data", max_results=5)
    """
    db_path = catalog_path.with_suffix(".db")
    store = load_vector_store(db_path)
    if store is None:
        return []

    results = store.search(query, top_k=max_results)

    return [
        {
            "server": r.server_id,
            "name": r.name,
            "key": r.key,
            "description": r.description,
            "score": r.score,
        }
        for r in results
        if r.score > 0
    ]
