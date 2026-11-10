import uuid
from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response

from app.chat_service import run_chat_completion
from app.embed_service import build_embeddings, validate_workspace
from app.flow_map import flow_nodes, write_flow_map
from app.log_relay import Timer, bind_request, emit, unbind_request
from app.rag_service import search_workspace
from app.report_service import generate_report
from app.schemas import (
    CdpStep,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbedRequest,
    EmbedResponse,
    FlowMapRequest,
    FlowMapResponse,
    HealthResponse,
    PrdResult,
    RagChunkResult,
    RagSearchRequest,
    RagSearchResponse,
    ReportRequest,
    ReportResponse,
    SuiteCase,
    TestSuite,
    ValidateResponse,
)
from app.settings import settings
from app.storage import load_stored_chunks
from app.tursor_config import TursorConfigError

app = FastAPI(
    title="Tursor AI",
    description=(
        "FastAPI service for workspace embeddings, RAG search, and LLM chat/test generation. "
        "Interactive API docs: GET /docs (Swagger UI), GET /redoc (ReDoc)."
    ),
    version="0.1.0",
)


@app.middleware("http")
async def bind_trace(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Logs from this request go to the backend's trace (see app.log_relay)."""
    token = bind_request(
        request.headers.get("x-tursor-trace"),
        request.headers.get("x-tursor-log-sink"),
    )
    try:
        return await call_next(request)
    finally:
        unbind_request(token)


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
    timer = Timer()
    try:
        result = build_embeddings(body.directory_path)
        emit(
            "success",
            "context",
            f"Indexed {result.files_indexed} files into {result.chunks_indexed} chunks "
            f"({result.files_added} added, {result.files_updated} updated, "
            f"{result.files_removed} removed, {result.files_unchanged} unchanged).",
            meta={"model": result.model, "incremental": result.incremental},
            duration_ms=timer.ms(),
        )
        return result
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/flow-map", response_model=FlowMapResponse)
def build_flow_map(body: FlowMapRequest) -> FlowMapResponse:
    try:
        cfg = validate_workspace(body.directory_path)
        records = load_stored_chunks(cfg.embeddings_path)
        flow = write_flow_map(cfg, records)
    except TursorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Flow map model call failed: {exc}") from exc
    nodes = flow_nodes(flow)
    emit(
        "warn" if flow.get("failed_batches") else "success",
        "flowmap",
        f"Flow map written: {len(nodes)} screens, "
        f"{sum(1 for node in nodes if node.get('reachable'))} reachable from /, "
        f"{flow.get('described', 0)} described by the model.",
    )
    return FlowMapResponse(
        screens=len(nodes),
        reachable=sum(1 for node in nodes if node.get("reachable")),
        described=int(flow.get("described", 0)),
        failed_batches=int(flow.get("failed_batches", 0)),
        skipped=len(nodes) == 0,
    )


@app.post("/v1/report", response_model=ReportResponse)
async def build_report(body: ReportRequest) -> ReportResponse:
    timer = Timer()
    report = await generate_report(body)
    m = report.metrics
    emit(
        "success",
        "report",
        f"Report {report.filename}: {m.total} cases, {m.executed} executed, "
        f"{m.passed} passed, {m.failed} failed.",
        meta=m.model_dump(),
        duration_ms=timer.ms(),
    )
    return report


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
        requirement_ids = item.get("requirement_ids")
        parsed.append(
            SuiteCase(
                kind=kind,
                title=title.strip() if isinstance(title, str) and title.strip() else "Test case",
                explanation=explanation.strip() if isinstance(explanation, str) else "",
                steps=steps,
                requirement_ids=[
                    value for value in requirement_ids if isinstance(value, str)
                ]
                if isinstance(requirement_ids, list)
                else [],
            )
        )
    if not parsed:
        return None
    module_key = raw.get("module_key")
    module_name = raw.get("module_name")
    return TestSuite(
        feature=feature.strip(),
        module_key=module_key if isinstance(module_key, str) else "",
        module_name=module_name if isinstance(module_name, str) else "",
        cases=parsed,
    )


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
            prior_intent=body.intent,
            focus_paths=body.focus_paths,
            existing_suites=[suite.model_dump() for suite in body.existing_suites],
            pending_action=body.pending_action,
            pending_suite_id=body.pending_suite_id,
            active_suite_id=body.active_suite_id,
            prd=body.prd.model_dump() if body.prd else None,
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
        intent=str(result.get("intent") or ""),
        focus_paths=[
            path for path in result.get("focus_paths") or [] if isinstance(path, str)
        ],
        reuse_suite_id=str(result.get("reuse_suite_id") or ""),
        prd=PrdResult.model_validate(result["prd"]) if result.get("prd") else None,
        pending_action=str(result.get("pending_action") or ""),
        pending_suite_id=str(result.get("pending_suite_id") or ""),
        suite_check=result.get("suite_check"),
        suite_fingerprint=str(result.get("suite_fingerprint") or ""),
    )
