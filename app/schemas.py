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


class ValidateResponse(BaseModel):
    ok: bool
    directory_path: str
    config_path: str | None = None
    excluded: list[str] = Field(default_factory=list)
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "tursor-ai"
    port: int = 8000
