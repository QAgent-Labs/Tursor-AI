from __future__ import annotations

import math
from pathlib import Path

from app.embedder import embed_texts
from app.storage import StoredChunkRecord, load_stored_chunks
from app.tursor_config import TursorWorkspaceConfig, load_tursor_config


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_workspace(
    workspace_path: str,
    query: str,
    *,
    top_k: int = 20,
) -> list[dict[str, str | int | float]]:
    cfg = load_tursor_config(Path(workspace_path))
    chunks = load_stored_chunks(cfg.embeddings_path)
    if not chunks:
        return []

    query_vec = embed_texts([query.strip()])
    if not query_vec:
        return []

    q = query_vec[0]
    scored: list[tuple[float, StoredChunkRecord]] = []
    for chunk in chunks:
        score = _cosine_similarity(q, chunk.embedding)
        scored.append((score, chunk))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results: list[dict[str, str | int | float]] = []
    for score, chunk in scored[: max(1, top_k)]:
        results.append(
            {
                "path": chunk.path,
                "content": chunk.text,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "score": round(score, 4),
            },
        )
    return results


def ensure_embeddings_exist(cfg: TursorWorkspaceConfig) -> None:
    chunks_path = cfg.embeddings_path / "chunks.jsonl"
    if not chunks_path.is_file():
        raise ValueError(
            f"No embeddings found at {chunks_path}. Run POST /v1/embed first.",
        )
