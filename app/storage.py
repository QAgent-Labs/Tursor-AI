import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.chunking import TextChunk
from app.embedder import model_name


@dataclass(frozen=True)
class StoredChunkRecord:
    path: str
    chunk_index: int
    start_line: int
    end_line: int
    text: str
    embedding: list[float]


@dataclass(frozen=True)
class FileFingerprint:
    mtime_ns: int
    size: int
    content_hash: str


def load_manifest(embeddings_dir: Path) -> dict[str, Any] | None:
    manifest_path = embeddings_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def load_stored_chunks(embeddings_dir: Path) -> list[StoredChunkRecord]:
    chunks_path = embeddings_dir / "chunks.jsonl"
    if not chunks_path.is_file():
        return []

    records: list[StoredChunkRecord] = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        embedding = raw.get("embedding")
        if not isinstance(path, str) or not isinstance(embedding, list):
            continue
        records.append(
            StoredChunkRecord(
                path=path,
                chunk_index=int(raw.get("chunk_index", 0)),
                start_line=int(raw.get("start_line", 1)),
                end_line=int(raw.get("end_line", 1)),
                text=str(raw.get("text", "")),
                embedding=[float(v) for v in embedding],
            ),
        )
    return records


def write_embeddings(
    embeddings_dir: Path,
    *,
    workspace_root: Path,
    excluded: list[str],
    include_patterns: list[str],
    files_indexed: int,
    file_fingerprints: dict[str, FileFingerprint],
    chunks: list[TextChunk],
    vectors: list[list[float]],
    files_added: int = 0,
    files_updated: int = 0,
    files_removed: int = 0,
    files_unchanged: int = 0,
) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors length mismatch")

    embeddings_dir.mkdir(parents=True, exist_ok=True)

    existing = load_manifest(embeddings_dir)
    created_at = existing.get("created_at") if existing else None
    if not isinstance(created_at, str):
        created_at = datetime.now(UTC).isoformat()

    manifest: dict[str, Any] = {
        "version": 2,
        "created_at": created_at,
        "updated_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(workspace_root),
        "model": model_name(),
        "excluded": excluded,
        "include_patterns": include_patterns,
        "files_indexed": files_indexed,
        "chunks_indexed": len(chunks),
        "files_added": files_added,
        "files_updated": files_updated,
        "files_removed": files_removed,
        "files_unchanged": files_unchanged,
        "files": {
            rel_path: {
                "mtime_ns": fp.mtime_ns,
                "size": fp.size,
                "content_hash": fp.content_hash,
            }
            for rel_path, fp in sorted(file_fingerprints.items())
        },
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
