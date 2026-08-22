from __future__ import annotations

import hashlib
from pathlib import Path

from app.chunking import TextChunk, chunk_file
from app.embedder import embed_texts, model_name
from app.file_walker import iter_indexable_files
from app.schemas import EmbedResponse
from app.storage import (
    FileFingerprint,
    StoredChunkRecord,
    load_manifest,
    load_stored_chunks,
    write_embeddings,
)
from app.tursor_config import (
    TursorWorkspaceConfig,
    load_tursor_config,
)


def validate_workspace(directory_path: str) -> TursorWorkspaceConfig:
    return load_tursor_config(Path(directory_path))


def _file_fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return FileFingerprint(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        content_hash=digest,
    )


def _fingerprint_from_manifest(raw: object) -> FileFingerprint | None:
    if not isinstance(raw, dict):
        return None
    mtime_ns = raw.get("mtime_ns")
    size = raw.get("size")
    content_hash = raw.get("content_hash")
    if (
        isinstance(mtime_ns, int)
        and isinstance(size, int)
        and isinstance(content_hash, str)
    ):
        return FileFingerprint(
            mtime_ns=mtime_ns,
            size=size,
            content_hash=content_hash,
        )
    return None


def _chunks_to_records(
    chunks: list[TextChunk],
    vectors: list[list[float]],
) -> list[StoredChunkRecord]:
    records: list[StoredChunkRecord] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        records.append(
            StoredChunkRecord(
                path=chunk.relative_path,
                chunk_index=chunk.chunk_index,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                embedding=vector,
            ),
        )
    return records


def _records_to_chunks(records: list[StoredChunkRecord]) -> list[TextChunk]:
    return [
        TextChunk(
            relative_path=record.path,
            chunk_index=record.chunk_index,
            start_line=record.start_line,
            end_line=record.end_line,
            text=record.text,
        )
        for record in records
    ]


def _group_records_by_path(
    records: list[StoredChunkRecord],
) -> dict[str, list[StoredChunkRecord]]:
    grouped: dict[str, list[StoredChunkRecord]] = {}
    for record in records:
        grouped.setdefault(record.path, []).append(record)
    for path in grouped:
        grouped[path].sort(key=lambda item: item.chunk_index)
    return grouped


def build_embeddings(directory_path: str) -> EmbedResponse:
    cfg = load_tursor_config(Path(directory_path))
    embeddings_dir = cfg.embeddings_path

    existing_manifest = load_manifest(embeddings_dir)
    existing_files_raw = (
        existing_manifest.get("files") if existing_manifest else None
    )
    existing_files: dict[str, FileFingerprint] = {}
    if isinstance(existing_files_raw, dict):
        for rel_path, raw_fp in existing_files_raw.items():
            if not isinstance(rel_path, str):
                continue
            fp = _fingerprint_from_manifest(raw_fp)
            if fp is not None:
                existing_files[rel_path] = fp

    existing_by_path = _group_records_by_path(load_stored_chunks(embeddings_dir))

    current_paths: set[str] = set()
    merged_records_by_path: dict[str, list[StoredChunkRecord]] = {}
    pending_by_path: dict[str, list[TextChunk]] = {}
    next_fingerprints: dict[str, FileFingerprint] = {}

    files_added = 0
    files_updated = 0
    files_unchanged = 0

    for file_path in iter_indexable_files(cfg):
        rel = str(file_path.relative_to(cfg.workspace_root)).replace("\\", "/")
        current_paths.add(rel)
        fingerprint = _file_fingerprint(file_path)
        next_fingerprints[rel] = fingerprint

        previous_fp = existing_files.get(rel)
        if previous_fp == fingerprint and rel in existing_by_path:
            merged_records_by_path[rel] = existing_by_path[rel]
            files_unchanged += 1
            continue

        if previous_fp is None:
            files_added += 1
        else:
            files_updated += 1

        file_chunks = chunk_file(file_path, cfg.workspace_root)
        pending_by_path[rel] = file_chunks
        merged_records_by_path[rel] = []

    files_removed = len(set(existing_files.keys()) - current_paths)

    pending_chunks: list[TextChunk] = []
    for rel in sorted(pending_by_path.keys()):
        pending_chunks.extend(pending_by_path[rel])

    new_vectors = embed_texts([chunk.text for chunk in pending_chunks])
    vector_index = 0
    for rel in sorted(pending_by_path.keys()):
        chunks = pending_by_path[rel]
        count = len(chunks)
        merged_records_by_path[rel] = _chunks_to_records(
            chunks,
            new_vectors[vector_index : vector_index + count],
        )
        vector_index += count

    final_records: list[StoredChunkRecord] = []
    for rel in sorted(merged_records_by_path.keys()):
        final_records.extend(merged_records_by_path[rel])

    final_chunks = _records_to_chunks(final_records)
    final_vectors = [record.embedding for record in final_records]

    write_embeddings(
        embeddings_dir,
        workspace_root=cfg.workspace_root,
        excluded=sorted(cfg.excluded),
        include_patterns=sorted(cfg.include_patterns),
        files_indexed=len(current_paths),
        file_fingerprints=next_fingerprints,
        chunks=final_chunks,
        vectors=final_vectors,
        files_added=files_added,
        files_updated=files_updated,
        files_removed=files_removed,
        files_unchanged=files_unchanged,
    )

    return EmbedResponse(
        directory_path=str(cfg.workspace_root),
        embeddings_dir=str(embeddings_dir),
        files_indexed=len(current_paths),
        chunks_indexed=len(final_chunks),
        model=model_name(),
        files_added=files_added,
        files_updated=files_updated,
        files_removed=files_removed,
        files_unchanged=files_unchanged,
        incremental=bool(existing_manifest),
    )
