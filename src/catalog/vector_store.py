"""Vector store: embed MCP tools with fastembed and store in SQLite."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import sqlite3
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

    from fastembed import TextEmbedding  # type: ignore[import-untyped]

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
    embedding   BLOB NOT NULL,
    text_hash   TEXT NOT NULL DEFAULT ''
)
"""


@dataclass
class SearchResult:
    """One ranked tool returned by VectorStore.search."""

    key: str
    server_id: str
    name: str
    description: str
    score: float  # 0-100


def _get_embedder() -> TextEmbedding:
    """Return a TextEmbedding instance (lazy import)."""
    # why: fastembed import and model load are slow; defer until actually needed.
    from fastembed import (  # type: ignore[import-untyped]  # pylint: disable=import-outside-toplevel
        TextEmbedding,
    )

    return TextEmbedding(_MODEL_ID)


def _hash_text(text: str) -> str:
    """Hash the embedded text together with the model id.

    why: including _MODEL_ID makes a model swap invalidate every stored vector.
    """
    return hashlib.sha256(f"{_MODEL_ID}\n{text}".encode()).hexdigest()


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open *db_path*, creating the table or migrating a legacy one."""
    con = sqlite3.connect(db_path)
    con.execute(_CREATE_TABLE)
    columns = {row[1] for row in con.execute("PRAGMA table_info(tool_embeddings)")}
    if "text_hash" not in columns:
        # why: legacy rows get an empty hash, so they are re-embedded exactly once.
        con.execute(
            "ALTER TABLE tool_embeddings ADD COLUMN text_hash TEXT NOT NULL DEFAULT ''"
        )
    return con


def build_index(catalog: Catalog, db_path: Path) -> None:
    """Embed new or changed tools in *catalog* and sync SQLite at *db_path*.

    Tools whose text hash matches the stored one keep their embedding (the
    embedder is not even loaded when nothing changed); tools no longer in the
    catalog are deleted.
    Example: build_index(catalog, Path("catalog.db"))
    """
    tools = catalog.all_tools()
    if not tools:
        logger.warning("build_index: catalog has no tools, skipping")
        return

    texts = {t.key: f"{t.name}: {t.description}" for t in tools}
    con = _open_db(db_path)
    try:
        stored = dict(con.execute("SELECT key, text_hash FROM tool_embeddings"))
        stale = [t for t in tools if stored.get(t.key) != _hash_text(texts[t.key])]
        if stale:
            logger.info("Embedding %d tools with %s", len(stale), _MODEL_ID)
            vectors = _get_embedder().embed([texts[t.key] for t in stale])
            con.executemany(
                "INSERT OR REPLACE INTO tool_embeddings"
                " (key, server_id, name, description, embedding, text_hash)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (
                        t.key,
                        t.server_id,
                        t.name,
                        t.description,
                        np.array(v, dtype=np.float32).tobytes(),
                        _hash_text(texts[t.key]),
                    )
                    for t, v in zip(stale, vectors, strict=True)
                ],
            )
        removed = [(k,) for k in stored.keys() - texts.keys()]
        con.executemany("DELETE FROM tool_embeddings WHERE key = ?", removed)
        con.commit()
    finally:
        con.close()

    logger.info(
        "Vector index written to %s: %d reused, %d embedded, %d removed",
        db_path,
        len(texts) - len({t.key for t in stale}),
        len(stale),
        len(removed),
    )


class VectorStore:
    """In-memory cosine-similarity search over pre-built SQLite embeddings.

    Example:
        store = VectorStore.load(Path("catalog.db"))
        results = store.search("retrieve data", top_k=5)
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        keys: list[str],
        server_ids: list[str],
        names: list[str],
        descriptions: list[str],
        matrix: np.ndarray,
    ) -> None:
        """Store tool metadata and an L2-normalized copy of *matrix* [N, D]."""
        self._keys = keys
        self._server_ids = server_ids
        self._names = names
        self._descriptions = descriptions
        # Shape [N, D], normalized for fast cosine via dot product
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = matrix / norms  # already float32

    @classmethod
    def load(cls, db_path: Path) -> VectorStore:
        """Load all embeddings from *db_path* into memory."""
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT key, server_id, name, description, embedding"
                " FROM tool_embeddings"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            raise ValueError(f"No tool embeddings found in {db_path}")

        keys, server_ids, names, descs, blobs = zip(*rows, strict=True)
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
