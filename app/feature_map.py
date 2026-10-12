"""A feature map is written once embeddings exist, then the closest entries are sent with each chat turn."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from app.embedder import embed_texts
from app.rag_service import _cosine_similarity
from app.storage import StoredChunkRecord
from app.tursor_config import TursorWorkspaceConfig

MAP_FILE = "feature-map.json"
_DIGEST_LIMIT = 100_000
_FILE_LIMIT = 6_000

_MAP_PROMPT = """You are reading a frontend codebase. List the user-facing features.

Respond with one JSON object and no markdown fences:
{
  "features": [
    {
      "feature": "short name",
      "details": "the fields, the button label on each step, and error text that stays on the page",
      "howToEnter": "the clicks from the first page, one screen at a time, using the labels in the code",
      "dependencies": ["relative/path.tsx"]
    }
  ]
}

dependencies are the files that implement the feature, including a shared component a page renders. Use only paths that appear in the source. A confirm field uses the same value as the field it must match. A button whose label changes by step is described with the label for each step.
"""


def map_path(embeddings_dir: Path) -> Path:
    return embeddings_dir / MAP_FILE


def load_feature_map(embeddings_dir: Path) -> list[dict[str, Any]]:
    path = map_path(embeddings_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    features = raw.get("features") if isinstance(raw, dict) else None
    if not isinstance(features, list):
        return []
    return [item for item in features if isinstance(item, dict)]


def _grouped_text(records: list[StoredChunkRecord]) -> dict[str, str]:
    grouped: dict[str, list[StoredChunkRecord]] = {}
    for record in records:
        grouped.setdefault(record.path, []).append(record)
    texts: dict[str, str] = {}
    for path, items in grouped.items():
        items.sort(key=lambda item: item.chunk_index)
        texts[path] = "\n".join(item.text for item in items)
    return texts


def _digest(texts: dict[str, str]) -> str:
    parts: list[str] = []
    used = 0
    for path in sorted(texts):
        if not path.endswith((".tsx", ".ts", ".jsx", ".js")):
            continue
        body = texts[path]
        if len(body) > _FILE_LIMIT:
            body = body[:_FILE_LIMIT]
        piece = f"--- {path} ---\n{body}"
        if used + len(piece) > _DIGEST_LIMIT:
            break
        parts.append(piece)
        used += len(piece)
    return "\n\n".join(parts)


def _parse_features(content: str, known_paths: set[str]) -> list[dict[str, Any]]:
    data = json.loads(content)
    raw_features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(raw_features, list):
        return []
    features: list[dict[str, Any]] = []
    for item in raw_features:
        if not isinstance(item, dict):
            continue
        feature = item.get("feature")
        details = item.get("details")
        how_to_enter = item.get("howToEnter")
        dependencies = item.get("dependencies")
        if not all(isinstance(value, str) and value.strip() for value in (feature, details, how_to_enter)):
            continue
        if not isinstance(dependencies, list):
            continue
        paths = [path for path in dependencies if isinstance(path, str) and path in known_paths]
        features.append(
            {
                "feature": feature.strip(),
                "details": details.strip(),
                "howToEnter": how_to_enter.strip(),
                "dependencies": paths,
            },
        )
    return features


def _attach_embeddings(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not features:
        return []
    vectors = embed_texts(
        [f"{item['feature']}\n{item['details']}\n{item['howToEnter']}" for item in features],
    )
    stored: list[dict[str, Any]] = []
    for item, vector in zip(features, vectors, strict=True):
        stored.append({**item, "embedding": vector})
    return stored


def write_feature_map(
    cfg: TursorWorkspaceConfig,
    records: list[StoredChunkRecord],
    *,
    openai_base_url: str = "https://api.openai.com/v1",
) -> list[dict[str, Any]]:
    if cfg.ai is None:
        return []
    if os.environ.get("TURSOR_AI_MOCK_LLM", "").strip() in {"1", "true", "yes"}:
        return []
    texts = _grouped_text(records)
    digest = _digest(texts)
    if not digest:
        return []
    payload = {
        "model": cfg.ai.generation_model,
        "messages": [
            {"role": "system", "content": _MAP_PROMPT},
            {"role": "user", "content": digest},
        ],
        "temperature": 1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {cfg.ai.api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=180.0) as client:
        res = client.post(
            f"{openai_base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
    res.raise_for_status()
    content = res.json()["choices"][0]["message"]["content"]
    features = _attach_embeddings(_parse_features(content, set(texts)))
    path = map_path(cfg.embeddings_path)
    path.write_text(
        json.dumps({"features": features}, indent=2),
        encoding="utf-8",
    )
    return features


def select_features(
    features: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    usable = [item for item in features if isinstance(item.get("embedding"), list)]
    if not usable or not query.strip():
        return []
    query_vec = embed_texts([query.strip()])
    if not query_vec:
        return []
    ranked = sorted(
        usable,
        key=lambda item: _cosine_similarity(query_vec[0], item["embedding"]),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for item in ranked[:top_k]:
        selected.append(
            {
                "feature": item.get("feature", ""),
                "details": item.get("details", ""),
                "howToEnter": item.get("howToEnter", ""),
                "dependencies": item.get("dependencies", []),
            },
        )
    return selected


def dependency_chunks(
    records: list[StoredChunkRecord],
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    paths: set[str] = set()
    for feature in features:
        for path in feature.get("dependencies") or []:
            if isinstance(path, str):
                paths.add(path)
    chunks: list[dict[str, Any]] = []
    for record in records:
        if record.path not in paths:
            continue
        chunks.append(
            {
                "path": record.path,
                "content": record.text,
                "start_line": record.start_line,
                "end_line": record.end_line,
            },
        )
    chunks.sort(key=lambda item: (str(item["path"]), int(item["start_line"])))
    return chunks
