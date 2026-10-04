"""Tests for semantic retrieval over workspace embeddings."""

from pathlib import Path

import pytest

from app.embed_service import build_embeddings
from app.rag_service import search_workspace
from app.settings import settings


@pytest.fixture(autouse=True)
def hash_mode() -> None:
    settings.hash_embeddings = True


def _write_config(tmp_path: Path) -> None:
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text(
        '{"excluded": [], "include": {"patterns": ["src/**"]}}',
        encoding="utf-8",
    )


def test_rag_search_returns_ranked_chunks(tmp_path: Path) -> None:
    _write_config(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "checkout.tsx").write_text(
        "export function Checkout() { return 'checkout auth token'; }\n",
        encoding="utf-8",
    )
    (src / "home.tsx").write_text(
        "export function Home() { return 'welcome home'; }\n",
        encoding="utf-8",
    )

    build_embeddings(str(tmp_path))

    hits = search_workspace(str(tmp_path), "checkout authentication", top_k=3)
    assert len(hits) >= 1
    assert any("checkout" in hit["path"] for hit in hits)
    assert hits[0]["score"] >= hits[-1]["score"]


def test_rag_search_empty_without_embeddings(tmp_path: Path) -> None:
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const x = 1;\n")

    hits = search_workspace(str(tmp_path), "anything")
    assert hits == []
