from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from typing import TYPE_CHECKING

from app.settings import settings

if TYPE_CHECKING:
    from fastembed import TextEmbedding


def _hash_embed_text(text: str) -> list[float]:
    dim = settings.hash_embedding_dim
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
        for i in range(0, len(block), 4):
            if len(out) >= dim:
                break
            chunk = int.from_bytes(block[i : i + 4], "big", signed=False)
            out.append((chunk / 2**32) * 2.0 - 1.0)
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


@lru_cache(maxsize=1)
def _embedding_model() -> TextEmbedding:
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=settings.embed_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    if settings.hash_embeddings:
        return [_hash_embed_text(t) for t in texts]

    model = _embedding_model()
    vectors: list[list[float]] = []
    for batch in model.embed(texts):
        if batch.ndim == 1:
            vectors.append(batch.tolist())
        else:
            for row in batch:
                vectors.append(row.tolist())
    if len(vectors) != len(texts):
        raise ValueError(
            f"Expected {len(texts)} embeddings, got {len(vectors)}",
        )
    return vectors


def model_name() -> str:
    if settings.hash_embeddings:
        return f"hash-{settings.hash_embedding_dim}"
    return settings.embed_model
