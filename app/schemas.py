from typing import Any, Literal

from pydantic import BaseModel, Field


class EmbedRequest(BaseModel):
    directory_path: str = Field(
        ...,
        description="Absolute path to the workspace root to index",
        min_length=1,
    )


class EmbedResponse(BaseModel):
    ok: bool = True
    directory_path: str
    embeddings_dir: str
    files_indexed: int
    chunks_indexed: int
    model: str
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    incremental: bool = False
    flow_map_needed: bool = False


class FlowMapRequest(BaseModel):
    directory_path: str = Field(..., min_length=1)


class FlowMapResponse(BaseModel):
    ok: bool = True
    screens: int = 0
    reachable: int = 0
    described: int = 0
    failed_batches: int = 0
    skipped: bool = False


class ValidateResponse(BaseModel):
    ok: bool
    directory_path: str
    config_path: str | None = None
    excluded: list[str] = Field(default_factory=list)
    include_patterns: list[str] = Field(default_factory=list)
    generation_model: str | None = None
    ai_configured: bool = False
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "tursor-ai"
    port: int = 8000


class RagSearchRequest(BaseModel):
    directory_path: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=20, ge=1, le=30)


class RagChunkResult(BaseModel):
    path: str
    content: str
    start_line: int
    end_line: int
    score: float


class RagSearchResponse(BaseModel):
    ok: bool = True
    directory_path: str
    query: str
    chunks: list[RagChunkResult] = Field(default_factory=list)


class PlanRef(BaseModel):
    id: str
    title: str = ""
    response_id: str = ""
    feature: str = ""
    kind: str = ""


class CdpRunRef(BaseModel):
    cdp_step_id: str
    status: Literal["passed", "failure"]
    status_message: str = ""
    response_id: str = ""
    feature: str = ""
    case_id: str = ""
    title: str = ""
    kind: str = ""


class CdpStep(BaseModel):
    id: str
    label: str
    actions: list[dict]


class LatestSuiteCase(BaseModel):
    id: str
    kind: str = ""
    title: str = ""
    steps: list[CdpStep] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)


class LatestTestSuite(BaseModel):
    response_id: str = ""
    suite_id: str = ""
    module_key: str = ""
    version: int = 0
    feature: str = ""
    cases: list[LatestSuiteCase] = Field(default_factory=list)


class ExistingSuiteCase(BaseModel):
    id: str = ""
    title: str = ""
    kind: str = ""
    last_status: str = ""
    # The latest finished run: run_id, status, finished_at, failed_step, status_message.
    last_run: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class ExistingSuite(BaseModel):
    """Latest version of a module's suite, so chat can check it and offer to reuse it."""

    suite_id: str = Field(..., min_length=1)
    module_key: str = ""
    module_name: str = ""
    feature: str = ""
    version: int = 1
    created_at: str = ""
    cases: list[ExistingSuiteCase] = Field(default_factory=list)
    # The last check against the code; reused while the flow's files are unchanged.
    checked_fingerprint: str = ""
    check_result: dict[str, Any] | None = None


class PrdRequirementRef(BaseModel):
    id: str
    text: str = ""
    status: str = ""


class PrdContext(BaseModel):
    prd_id: str = ""
    title: str = ""
    requirements: list[PrdRequirementRef] = Field(default_factory=list)


class PrdRequirementResult(BaseModel):
    id: str
    text: str
    status: Literal["implemented", "partial", "missing"]
    evidence: str = ""
    gap: str = ""


class PrdResult(BaseModel):
    title: str
    verdict: Literal["aligned", "gaps"]
    content: str = ""
    requirements: list[PrdRequirementResult]


class SuiteCase(BaseModel):
    kind: Literal["success", "failure", "edge"]
    title: str
    explanation: str = ""
    steps: list[CdpStep]
    requirement_ids: list[str] = Field(default_factory=list)


class TestSuite(BaseModel):
    feature: str
    module_key: str = ""
    module_name: str = ""
    cases: list[SuiteCase]


class ChatCompletionRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1)
    workspace_path: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    generation_model: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    mode: Literal["chat", "intro"] = "chat"
    case: str = ""
    brief_summary: str = ""
    plans: list[PlanRef] = Field(default_factory=list)
    cdp_runs: list[CdpRunRef] = Field(default_factory=list)
    latest_cdp_steps: list[CdpStep] | None = None
    latest_test_suite: LatestTestSuite | None = None
    intent: str = ""
    focus_paths: list[str] = Field(default_factory=list)
    existing_suites: list[ExistingSuite] = Field(default_factory=list)
    # What Tursor asked last turn ("generate", "reuse", "new_version") and for which suite.
    pending_action: str = ""
    pending_suite_id: str = ""
    active_suite_id: str = ""
    prd: PrdContext | None = None


class ChatCompletionResponse(BaseModel):
    conversation_id: str
    response_id: str
    reply: str
    case: str = ""
    brief_summary: str = ""
    test_suite: TestSuite | None = None
    retrieved_chunk_count: int = 0
    intent: str = ""
    focus_paths: list[str] = Field(default_factory=list)
    reuse_suite_id: str = ""
    prd: PrdResult | None = None
    pending_action: str = ""
    pending_suite_id: str = ""
    # Verdict of checking a saved suite against the code; the backend stores it.
    suite_check: dict[str, Any] | None = None
    # Fingerprint of the files behind a newly generated suite: valid when created.
    suite_fingerprint: str = ""


class ReportRun(BaseModel):
    run_id: str = ""
    status: Literal["passed", "failure"]
    status_message: str = ""
    failed_step: str = ""
    run_folder: str = ""
    screenshots: list[str] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


class ReportCase(BaseModel):
    case_id: str
    kind: str = ""
    title: str = ""
    explanation: str = ""
    requirement_ids: list[str] = Field(default_factory=list)
    # CDP steps as stored: {id, label, actions}.
    steps: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[ReportRun] = Field(default_factory=list)


class ReportSuite(BaseModel):
    suite_id: str = Field(..., min_length=1)
    module_key: str = ""
    module_name: str = ""
    feature: str = ""
    version: int = 1
    created_at: str = ""
    cases: list[ReportCase] = Field(default_factory=list)


class ReportPrdRequirement(BaseModel):
    id: str
    text: str = ""
    status: str = ""
    gap: str = ""


class ReportPrd(BaseModel):
    title: str = ""
    # The spec check's verdict: "aligned" or "gaps".
    verdict: str = ""
    requirements: list[ReportPrdRequirement] = Field(default_factory=list)


class ReportRequest(BaseModel):
    format: Literal["md", "pdf"] = "md"
    # Used to read the flow map so the report can explain the feature.
    workspace_path: str = ""
    generation_model: str = ""
    api_key: str = ""
    suite: ReportSuite
    prd: ReportPrd | None = None


class ReportMetrics(BaseModel):
    total: int
    executed: int
    passed: int
    failed: int
    not_run: int
    runs: int


class ReportResponse(BaseModel):
    ok: bool = True
    format: Literal["md", "pdf"]
    filename: str
    content_type: str
    encoding: Literal["utf-8", "base64"]
    content: str
    metrics: ReportMetrics
