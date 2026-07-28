from pathlib import Path

from app.chunking import TextChunk, chunk_file
from app.embedder import embed_texts, model_name
from app.file_walker import iter_indexable_files
from app.schemas import EmbedResponse
from app.storage import write_embeddings
from app.tursor_config import (
    TursorWorkspaceConfig,
    load_tursor_config,
)


def validate_workspace(directory_path: str) -> TursorWorkspaceConfig:
    return load_tursor_config(Path(directory_path))


def build_embeddings(directory_path: str) -> EmbedResponse:
    cfg = load_tursor_config(Path(directory_path))
    all_chunks: list[TextChunk] = []
    files_seen: set[str] = set()

    for file_path in iter_indexable_files(cfg):
        file_chunks = chunk_file(file_path, cfg.workspace_root)
        if file_chunks:
            rel = file_chunks[0].relative_path
            files_seen.add(rel)
            all_chunks.extend(file_chunks)

    texts = [c.text for c in all_chunks]
    vectors = embed_texts(texts)

    embeddings_dir = cfg.embeddings_path
    if embeddings_dir.exists():
        for child in embeddings_dir.iterdir():
            if child.is_file():
                child.unlink()
    write_embeddings(
        embeddings_dir,
        workspace_root=cfg.workspace_root,
        excluded=sorted(cfg.excluded),
        files_indexed=len(files_seen),
        chunks=all_chunks,
        vectors=vectors,
    )

    return EmbedResponse(
        directory_path=str(cfg.workspace_root),
        embeddings_dir=str(embeddings_dir),
        files_indexed=len(files_seen),
        chunks_indexed=len(all_chunks),
        model=model_name(),
    )
