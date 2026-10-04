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


@app.post("/v1/chat/completion", response_model=ChatCompletionResponse)
async def chat_completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    latest = None
    if body.latest_cdp_steps:
        latest = [step.model_dump() for step in body.latest_cdp_steps]

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
        )
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    steps = None
    raw_steps = result.get("cdp_steps")
    if isinstance(raw_steps, list) and raw_steps:
        steps = [
            CdpStep(
                id=str(step["id"]),
                label=str(step["label"]),
                actions=list(step["actions"]),
            )
            for step in raw_steps
            if isinstance(step, dict)
        ]

    return ChatCompletionResponse(
        reply=str(result.get("reply") or ""),
        case=str(result.get("case") or ""),
        brief_summary=str(result.get("brief_summary") or ""),
        cdp_steps=steps or None,
        retrieved_chunk_count=int(result.get("retrieved_chunk_count", 0)),
    )
