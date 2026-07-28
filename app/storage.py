import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.chunking import TextChunk
from app.embedder import model_name


def write_embeddings(
    embeddings_dir: Path,
    *,
    workspace_root: Path,
    excluded: list[str],
    files_indexed: int,
    chunks: list[TextChunk],
    vectors: list[list[float]],
) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors length mismatch")

    embeddings_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(workspace_root),
        "model": model_name(),
        "excluded": excluded,
        "files_indexed": files_indexed,
        "chunks_indexed": len(chunks),
    }
    (embeddings_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    chunks_path = embeddings_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as out:
        for chunk, vector in zip(chunks, vectors, strict=True):
            record = {
                "path": chunk.relative_path,
                "chunk_index": chunk.chunk_index,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text,
                "embedding": vector,
            }
            out.write(json.dumps(record, ensure_ascii=False))
            out.write("\n")
