from typing import Literal

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
    top_k: int = Field(default=8, ge=1, le=30)


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


class CdpRunRef(BaseModel):
    cdp_step_id: str
    status: Literal["passed", "failure"]
    status_message: str = ""


class CdpStep(BaseModel):
    id: str
    label: str
    actions: list[dict]


class ChatCompletionRequest(BaseModel):
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


class ChatCompletionResponse(BaseModel):
    reply: str
    case: str = ""
    brief_summary: str = ""
    cdp_steps: list[CdpStep] | None = None
    retrieved_chunk_count: int = 0
