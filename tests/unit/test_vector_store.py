"""Unit tests for incremental build_index (embeddings reused by text hash)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
import zlib

import numpy as np
import pytest

from src.catalog import vector_store
from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.catalog.vector_store import build_index

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

EMBEDDING_DIM = 384


class FakeEmbedder:  # pylint: disable=too-few-public-methods
    """Deterministic embedder: vector derived from text; records embedded texts."""

    def __init__(self, stats: dict) -> None:
        self._stats = stats
        stats["instances"] += 1

    def embed(self, texts: list[str]) -> Iterator[np.ndarray]:
        self._stats["embed_calls"] += 1
        self._stats["texts"].extend(texts)
        for text in texts:
            rng = np.random.default_rng(zlib.crc32(text.encode()))
            yield rng.random(EMBEDDING_DIM, dtype=np.float32)


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> dict:
    data: dict = {"instances": 0, "embed_calls": 0, "texts": []}
    monkeypatch.setattr(vector_store, "_get_embedder", lambda: FakeEmbedder(data))
    return data


def _reset(stats: dict) -> None:
    stats.update(instances=0, embed_calls=0, texts=[])


def _catalog(tools: dict[str, str], server_id: str = "srv") -> Catalog:
    return Catalog(
        backends=[
            CatalogBackend(
                id=server_id,
                name=server_id,
                type="http",
                tools=[
                    CatalogTool(server_id=server_id, name=n, description=d)
                    for n, d in tools.items()
                ],
            )
        ]
    )


def _rows(db: Path) -> dict[str, tuple]:
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT * FROM tool_embeddings").fetchall()
    return {r[0]: r[1:] for r in rows}


TOOLS = {"alpha": "first tool", "beta": "second tool", "gamma": "third tool"}


def test_first_build_embeds_all(tmp_path: Path, stats: dict) -> None:
    build_index(_catalog(TOOLS), tmp_path / "c.db")
    assert sorted(stats["texts"]) == sorted(f"{n}: {d}" for n, d in TOOLS.items())
    assert set(_rows(tmp_path / "c.db")) == {"srv/alpha", "srv/beta", "srv/gamma"}


def test_unchanged_catalog_skips_embedder(tmp_path: Path, stats: dict) -> None:
    db = tmp_path / "c.db"
    build_index(_catalog(TOOLS), db)
    before = _rows(db)
    _reset(stats)

    build_index(_catalog(TOOLS), db)

    assert stats["instances"] == 0
    assert stats["embed_calls"] == 0
    assert _rows(db) == before


def test_changed_description_reembeds_only_that_tool(
    tmp_path: Path, stats: dict
) -> None:
    db = tmp_path / "c.db"
    build_index(_catalog(TOOLS), db)
    before = _rows(db)
    _reset(stats)

    build_index(_catalog({**TOOLS, "beta": "second tool, now better"}), db)

    after = _rows(db)
    assert stats["texts"] == ["beta: second tool, now better"]
    assert after["srv/beta"][2] == "second tool, now better"
    assert after["srv/beta"][3] != before["srv/beta"][3]
    assert after["srv/alpha"] == before["srv/alpha"]


def test_new_tool_embeds_only_new(tmp_path: Path, stats: dict) -> None:
    db = tmp_path / "c.db"
    build_index(_catalog(TOOLS), db)
    _reset(stats)

    build_index(_catalog({**TOOLS, "delta": "fourth tool"}), db)

    assert stats["texts"] == ["delta: fourth tool"]
    assert "srv/delta" in _rows(db)


def test_removed_tool_deleted(tmp_path: Path, stats: dict) -> None:
    db = tmp_path / "c.db"
    build_index(_catalog(TOOLS), db)
    _reset(stats)

    build_index(_catalog({"alpha": "first tool", "beta": "second tool"}), db)

    assert stats["instances"] == 0
    assert set(_rows(db)) == {"srv/alpha", "srv/beta"}


def test_legacy_db_without_hash_migrates_and_reembeds_once(
    tmp_path: Path, stats: dict
) -> None:
    db = tmp_path / "c.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tool_embeddings (key TEXT PRIMARY KEY, server_id TEXT NOT NULL,"
        " name TEXT NOT NULL, description TEXT NOT NULL, embedding BLOB NOT NULL)"
    )
    con.execute(
        "INSERT INTO tool_embeddings VALUES (?,?,?,?,?)",
        ("srv/alpha", "srv", "alpha", "first tool", b"\x00" * 4 * EMBEDDING_DIM),
    )
    con.commit()
    con.close()

    build_index(_catalog(TOOLS), db)
    assert len(stats["texts"]) == len(TOOLS)
    _reset(stats)

    build_index(_catalog(TOOLS), db)
    assert stats["embed_calls"] == 0
    assert vector_store.VectorStore.load(db) is not None


def test_model_change_reembeds_all(
    tmp_path: Path, stats: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    build_index(_catalog(TOOLS), db)
    _reset(stats)

    monkeypatch.setattr(vector_store, "_MODEL_ID", "other/model")
    build_index(_catalog(TOOLS), db)

    assert len(stats["texts"]) == len(TOOLS)


def test_incremental_result_equals_full_rebuild(tmp_path: Path, stats: dict) -> None:
    final = {"alpha": "first tool", "beta": "changed", "delta": "new"}
    incremental = tmp_path / "inc.db"
    build_index(_catalog(TOOLS), incremental)
    build_index(_catalog(final), incremental)

    full = tmp_path / "full.db"
    build_index(_catalog(final), full)

    assert _rows(incremental) == _rows(full)
