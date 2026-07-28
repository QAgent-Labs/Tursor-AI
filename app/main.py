from fastapi import FastAPI, HTTPException, Query

from app.embed_service import build_embeddings, validate_workspace
from app.schemas import EmbedRequest, EmbedResponse, HealthResponse, ValidateResponse
from app.settings import settings
from app.tursor_config import TursorConfigError

app = FastAPI(
    title="Tursor AI",
    description="Workspace embeddings for future CDP step generation",
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
