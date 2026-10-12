from __future__ import annotations

from pathlib import Path
from typing import Any

from app.feature_map import dependency_chunks, load_feature_map, select_features
from app.intent_router import (
    FEATURE,
    GENERAL_QA,
    OUT_OF_SCOPE,
    OUT_OF_SCOPE_REPLY,
    TEST_GENERATION,
    features_named,
    is_confirmation,
    is_out_of_scope,
    names_a_flow,
    wants_generation,
)
from app.llm_service import LlmError, complete_chat
from app.prompts import SYSTEM_PROMPT
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
    rag_top_k: int = 50,
    prior_intent: str = "",
    focus_paths: list[str] | None = None,
) -> dict[str, Any]:
    cfg = load_tursor_config(Path(workspace_path))
    ensure_embeddings_exist(cfg)
    saved_paths = [path for path in (focus_paths or []) if isinstance(path, str) and path]

    if mode != "intro":
        catalog = load_feature_map(cfg.embeddings_path)
        names = [
            item["feature"]
            for item in catalog
            if isinstance(item.get("feature"), str)
        ]
        if is_out_of_scope(message):
            return {
                "reply": OUT_OF_SCOPE_REPLY,
                "case": case,
                "brief_summary": brief_summary,
                "test_suite": None,
                "retrieved_chunk_count": 0,
                "intent": OUT_OF_SCOPE,
                "focus_paths": [],
            }

        named = features_named(message, case, catalog)
        confirming = is_confirmation(message) and prior_intent in {FEATURE, TEST_GENERATION}
        generate = wants_generation(message) or confirming
        if not named and not generate and not names_a_flow(message, case):
            return await _complete(
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
                retrieved=[],
                features=[],
                feature_names=names,
                intent=GENERAL_QA,
                focus_paths=[],
                system_prompt=SYSTEM_PROMPT,
                allow_suite=False,
            )

        records = load_stored_chunks(cfg.embeddings_path)
        reuse = confirming and bool(saved_paths)
        if reuse:
            retrieved = dependency_chunks(records, [{"dependencies": saved_paths}])
            features = named
            next_paths = saved_paths
        else:
            query = _retrieval_query(mode, message, case)
            retrieved = search_workspace(workspace_path, query, top_k=rag_top_k) if query else []
            features = named
            retrieved = _with_dependency_chunks(records, features, retrieved, rag_top_k)
            next_paths = _dependency_paths(features) or saved_paths
        return await _complete(
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
            retrieved=retrieved,
            features=features,
            records=records,
            intent=TEST_GENERATION if generate else FEATURE,
            focus_paths=next_paths,
            system_prompt=SYSTEM_PROMPT,
            allow_suite=generate,
        )

    query = _retrieval_query(mode, message, case)
    retrieved = search_workspace(workspace_path, query, top_k=rag_top_k) if query else []
    records = load_stored_chunks(cfg.embeddings_path)
    features = select_features(load_feature_map(cfg.embeddings_path), query) if query else []
    retrieved = _with_dependency_chunks(records, features, retrieved, rag_top_k)

    return await _complete(
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
        retrieved=retrieved,
        features=features,
        records=records,
        intent="",
        focus_paths=[],
        system_prompt=SYSTEM_PROMPT,
        allow_suite=True,
    )


def _dependency_paths(features: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for feature in features:
        for path in feature.get("dependencies") or []:
            if isinstance(path, str) and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


async def _complete(
    *,
    generation_model: str,
    api_key: str,
    mode: str,
    message: str,
    case: str,
    brief_summary: str,
    plans: list[dict[str, Any]] | None,
    cdp_runs: list[dict[str, Any]] | None,
    latest_cdp_steps: list[dict[str, Any]] | None,
    latest_test_suite: dict[str, Any] | None,
    retrieved: list[dict[str, Any]],
    features: list[dict[str, Any]],
    feature_names: list[str] | None = None,
    intent: str,
    focus_paths: list[str],
    system_prompt: str,
    allow_suite: bool,
    records: list[Any] | None = None,
) -> dict[str, Any]:
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
            feature_names=feature_names,
            system_prompt=system_prompt,
            intent=intent,
        )
    except LlmError as exc:
        return {
            "reply": str(exc),
            "case": case,
            "brief_summary": brief_summary,
            "test_suite": None,
            "retrieved_chunk_count": len(retrieved),
            "intent": intent,
            "focus_paths": focus_paths,
        }

    if allow_suite:
        sources = [record.text for record in records or []]
        ai_response["test_suite"] = ground_suite(ai_response.get("test_suite"), sources)
    else:
        ai_response["test_suite"] = None
    return {
        **ai_response,
        "retrieved_chunk_count": len(retrieved),
        "intent": intent,
        "focus_paths": focus_paths,
    }
