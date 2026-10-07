import uuid

from fastapi import FastAPI, HTTPException, Query

from app.chat_service import run_chat_completion
from app.embed_service import build_embeddings, validate_workspace
from app.rag_service import search_workspace
from app.schemas import (
    CdpStep,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    RagChunkResult,
    RagSearchRequest,
    RagSearchResponse,
    SuiteCase,
    TestSuite,
    ValidateResponse,
)
from app.settings import settings
from app.tursor_config import TursorConfigError

app = FastAPI(
    title="Tursor AI",
    description=(
        "FastAPI service for workspace embeddings, RAG search, and LLM chat/test generation. "
        "Interactive API docs: GET /docs (Swagger UI), GET /redoc (ReDoc)."
    ),
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(port=settings.port)


@app.get("/v1/validate", response_model=ValidateResponse)
def validate_config(
    directory_path: str = Query(..., min_length=1),
) -> ValidateResponse:
    try:
        cfg = validate_workspace(directory_path)
    except TursorConfigError as exc:
        return ValidateResponse(
            ok=False,
            directory_path=directory_path,
            error=str(exc),
        )

    return ValidateResponse(
        ok=True,
        directory_path=str(cfg.workspace_root),
        config_path=str(cfg.config_path),
        excluded=sorted(cfg.excluded),
        include_patterns=sorted(cfg.include_patterns),
        generation_model=cfg.ai.generation_model if cfg.ai else None,
        ai_configured=cfg.ai is not None,
    )


@app.post("/v1/embed", response_model=EmbedResponse)
def embed_workspace(body: EmbedRequest) -> EmbedResponse:
    try:
        return build_embeddings(body.directory_path)
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/rag/search", response_model=RagSearchResponse)
def rag_search(body: RagSearchRequest) -> RagSearchResponse:
    try:
        raw = search_workspace(
            body.directory_path,
            body.query,
            top_k=body.top_k,
        )
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chunks = [
        RagChunkResult(
            path=str(item["path"]),
            content=str(item["content"]),
            start_line=int(item["start_line"]),
            end_line=int(item["end_line"]),
            score=float(item["score"]),
        )
        for item in raw
    ]
    return RagSearchResponse(
        directory_path=body.directory_path,
        query=body.query,
        chunks=chunks,
    )


def _suite_from_result(raw: object) -> TestSuite | None:
    if not isinstance(raw, dict):
        return None
    feature = raw.get("feature")
    cases = raw.get("cases")
    if not isinstance(feature, str) or not feature.strip() or not isinstance(cases, list):
        return None
    parsed: list[SuiteCase] = []
    for item in cases:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in {"success", "failure", "edge"}:
            continue
        steps_raw = item.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            continue
        steps = [
            CdpStep(
                id=str(step["id"]),
                label=str(step["label"]),
                actions=list(step["actions"]),
            )
            for step in steps_raw
            if isinstance(step, dict)
        ]
        if not steps:
            continue
        title = item.get("title")
        explanation = item.get("explanation")
        parsed.append(
            SuiteCase(
                kind=kind,
                title=title.strip() if isinstance(title, str) and title.strip() else "Test case",
                explanation=explanation.strip() if isinstance(explanation, str) else "",
                steps=steps,
            )
        )
    if not parsed:
        return None
    return TestSuite(feature=feature.strip(), cases=parsed)


@app.post("/v1/chat/completion", response_model=ChatCompletionResponse)
async def chat_completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    latest = None
    if body.latest_cdp_steps:
        latest = [step.model_dump() for step in body.latest_cdp_steps]
    latest_suite = body.latest_test_suite.model_dump() if body.latest_test_suite else None

    try:
        result = await run_chat_completion(
            workspace_path=body.workspace_path,
            message=body.message,
            generation_model=body.generation_model,
            api_key=body.api_key,
            mode=body.mode,
            case=body.case,
            brief_summary=body.brief_summary,
            plans=[plan.model_dump() for plan in body.plans],
            cdp_runs=[run.model_dump() for run in body.cdp_runs],
            latest_cdp_steps=latest,
            latest_test_suite=latest_suite,
        )
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ChatCompletionResponse(
        conversation_id=body.conversation_id,
        response_id=str(uuid.uuid4()),
        reply=str(result.get("reply") or ""),
        case=str(result.get("case") or ""),
        brief_summary=str(result.get("brief_summary") or ""),
        test_suite=_suite_from_result(result.get("test_suite")),
        retrieved_chunk_count=int(result.get("retrieved_chunk_count", 0)),
    )
