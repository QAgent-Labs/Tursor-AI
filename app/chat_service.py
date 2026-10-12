from __future__ import annotations

from pathlib import Path
from typing import Any

from app.feature_map import dependency_chunks, load_feature_map, select_features
from app.llm_service import LlmError, complete_chat
from app.rag_service import ensure_embeddings_exist, search_workspace
from app.selector_grounding import ground_suite
from app.storage import load_stored_chunks
from app.tursor_config import load_tursor_config


def _with_dependency_chunks(
    records: list[Any],
    features: list[dict[str, Any]],
    retrieved: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    required = dependency_chunks(records, features)
    seen = {(item.get("path"), item.get("start_line")) for item in required}
    merged = list(required)
    for item in retrieved:
        key = (item.get("path"), item.get("start_line"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:limit]


def _retrieval_query(mode: str, message: str, case: str) -> str:
    if mode == "intro":
        return "application routes pages"
    parts = [message.strip()]
    recorded = case.strip()
    if recorded:
        parts.append(recorded[:2000])
    return "\n".join(part for part in parts if part)


async def run_chat_completion(
    *,
    workspace_path: str,
    message: str,
    generation_model: str,
    api_key: str,
    mode: str = "chat",
    case: str = "",
    brief_summary: str = "",
    plans: list[dict[str, Any]] | None = None,
    cdp_runs: list[dict[str, Any]] | None = None,
    latest_cdp_steps: list[dict[str, Any]] | None = None,
    latest_test_suite: dict[str, Any] | None = None,
    rag_top_k: int = 100,
) -> dict[str, Any]:
    cfg = load_tursor_config(Path(workspace_path))
    ensure_embeddings_exist(cfg)

    query = _retrieval_query(mode, message, case)
    retrieved = search_workspace(workspace_path, query, top_k=rag_top_k) if query else []
    records = load_stored_chunks(cfg.embeddings_path)
    features = select_features(load_feature_map(cfg.embeddings_path), query) if query else []
    retrieved = _with_dependency_chunks(records, features, retrieved, rag_top_k)

    try:
        ai_response = await complete_chat(
            generation_model=generation_model,
            api_key=api_key,
            mode=mode,
            message=message,
            case=case,
            brief_summary=brief_summary,
            plans=plans,
            cdp_runs=cdp_runs,
            latest_cdp_steps=latest_cdp_steps,
            latest_test_suite=latest_test_suite,
            retrieved_context=retrieved,
            feature_map=features,
        )
    except LlmError as exc:
        return {
            "reply": str(exc),
            "case": case,
            "brief_summary": brief_summary,
            "test_suite": None,
            "retrieved_chunk_count": len(retrieved),
        }

    sources = [record.text for record in records]
    ai_response["test_suite"] = ground_suite(ai_response.get("test_suite"), sources)
    return {
        **ai_response,
        "retrieved_chunk_count": len(retrieved),
    }
