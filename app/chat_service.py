from __future__ import annotations

from pathlib import Path
from typing import Any

from app.llm_service import LlmError, complete_chat
from app.rag_service import ensure_embeddings_exist, search_workspace
from app.tursor_config import load_tursor_config


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
    rag_top_k: int = 20,
) -> dict[str, Any]:
    cfg = load_tursor_config(Path(workspace_path))
    ensure_embeddings_exist(cfg)

    query = message.strip() if mode != "intro" else "application routes pages"
    retrieved = search_workspace(workspace_path, query, top_k=rag_top_k) if query else []

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
        )
    except LlmError as exc:
        return {
            "reply": str(exc),
            "case": case,
            "brief_summary": brief_summary,
            "test_suite": None,
            "retrieved_chunk_count": len(retrieved),
        }

    return {
        **ai_response,
        "retrieved_chunk_count": len(retrieved),
    }
