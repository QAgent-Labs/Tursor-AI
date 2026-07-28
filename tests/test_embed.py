from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.embed_service import build_embeddings, validate_workspace
from app.main import app
from app.settings import settings
from app.tursor_config import TursorConfigError


@pytest.fixture(autouse=True)
def hash_mode() -> None:
    settings.hash_embeddings = True


def test_validate_missing_config(tmp_path: Path) -> None:
    with pytest.raises(TursorConfigError):
        validate_workspace(str(tmp_path))


def test_embed_writes_artifacts(tmp_path: Path) -> None:
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text(
        '{"excluded": ["node_modules", "dist"]}',
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    node = tmp_path / "node_modules"
    node.mkdir()
    (node / "ignored.js").write_text("ignored", encoding="utf-8")

    result = build_embeddings(str(tmp_path))
    assert result.files_indexed == 1
    assert result.chunks_indexed >= 1
    assert (tursor / "embeddings" / "manifest.json").is_file()
    assert (tursor / "embeddings" / "chunks.jsonl").is_file()

    cfg = validate_workspace(str(tmp_path))
    assert cfg.excluded == frozenset({"node_modules", "dist"})


def test_http_validate_and_embed(tmp_path: Path) -> None:
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    client = TestClient(app)
    validate_res = client.get(
        "/v1/validate",
        params={"directory_path": str(tmp_path)},
    )
    assert validate_res.status_code == 200
    assert validate_res.json()["ok"] is True

    embed_res = client.post(
        "/v1/embed",
        json={"directory_path": str(tmp_path)},
    )
    assert embed_res.status_code == 200
    body = embed_res.json()
    assert body["chunks_indexed"] >= 1
