"""Vector store: embed MCP tools with fastembed and store in SQLite."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.catalog.schema import Catalog

logger = logging.getLogger(__name__)

_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_EMBEDDING_DIM = 384

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tool_embeddings (
    key         TEXT PRIMARY KEY,
    server_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    embedding   BLOB NOT NULL
)
"""


@dataclass
class SearchResult:
    key: str
    server_id: str
    name: str
    description: str
    score: float  # 0-100


def _get_embedder():  # noqa: ANN201
    """Return a cached TextEmbedding instance (lazy import)."""
    from fastembed import TextEmbedding  # type: ignore[import-untyped]

    return TextEmbedding(_MODEL_ID)


def build_index(catalog: Catalog, db_path: Path) -> None:
    """Embed all tools in *catalog* and write to SQLite at *db_path*.

    Replaces existing rows via INSERT OR REPLACE.
    Example: build_index(catalog, Path("catalog.db"))
    """
    tools = catalog.all_tools()
    if not tools:
        logger.warning("build_index: catalog has no tools, skipping")
        return

    embedder = _get_embedder()
    texts = [f"{t.name}: {t.description}" for t in tools]

    logger.info("Embedding %d tools with %s", len(tools), _MODEL_ID)
    vectors = list(embedder.embed(texts))  # list of np.ndarray float32

    con = sqlite3.connect(db_path)
    try:
        con.execute(_CREATE_TABLE)
        rows = [
            (
                t.key,
                t.server_id,
                t.name,
                t.description,
                np.array(v, dtype=np.float32).tobytes(),
            )
            for t, v in zip(tools, vectors)
        ]
        con.executemany(
            "INSERT OR REPLACE INTO tool_embeddings VALUES (?,?,?,?,?)", rows
        )
        con.commit()
    finally:
        con.close()

    logger.info("Vector index written to %s (%d tools)", db_path, len(tools))


class VectorStore:
    """In-memory cosine-similarity search over pre-built SQLite embeddings.

    Example:
        store = VectorStore.load(Path("catalog.db"))
        results = store.search("retrieve data", top_k=5)
    """

    def __init__(
        self,
        keys: list[str],
        server_ids: list[str],
        names: list[str],
        descriptions: list[str],
        matrix: np.ndarray,
    ) -> None:
        self._keys = keys
        self._server_ids = server_ids
        self._names = names
        self._descriptions = descriptions
        # Shape [N, D], normalized for fast cosine via dot product
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = matrix / norms  # already float32

    @classmethod
    def load(cls, db_path: Path) -> "VectorStore":
        """Load all embeddings from *db_path* into memory."""
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT key, server_id, name, description, embedding FROM tool_embeddings"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            raise ValueError(f"No tool embeddings found in {db_path}")

        keys, server_ids, names, descs, blobs = zip(*rows)
        matrix = np.stack([np.frombuffer(b, dtype=np.float32) for b in blobs])
        return cls(list(keys), list(server_ids), list(names), list(descs), matrix)

    def search(self, query: str, top_k: int) -> list[SearchResult]:
        """Return up to *top_k* tools ranked by cosine similarity to *query*."""
        embedder = _get_embedder()
        (q_vec,) = list(embedder.embed([query]))
        q_arr = np.array(q_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q_arr)
        if q_norm > 0:
            q_arr /= q_norm

        sims = self._matrix @ q_arr  # shape [N]
        top_k = min(top_k, len(sims))
        indices = np.argpartition(sims, -top_k)[-top_k:]
        indices = indices[np.argsort(sims[indices])[::-1]]

        return [
            SearchResult(
                key=self._keys[i],
                server_id=self._server_ids[i],
                name=self._names[i],
                description=self._descriptions[i],
                score=round(float(sims[i]) * 100, 2),
            )
            for i in indices
        ]
