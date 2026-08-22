# Tursor-AI

FastAPI service that indexes a workspace and writes embeddings under `.tursor/embeddings/`.

## Setup

```bash
cd Tursor-AI
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Workspace config

Create `{workspace}/.tursor/config.json`:

```json
{
  "excluded": ["node_modules", "dist", ".git", "package-lock.json"]
}
```

Embeddings use [fastembed](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5` by default). First run downloads the model from Hugging Face.

For offline/tests set `TURSOR_AI_HASH_EMBEDDINGS=1` (deterministic hash vectors, not for production search quality).

## Run

```bash
python run.py
# or: uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## API

- `GET /health`
- `GET /v1/validate?directory_path=/abs/path/to/repo`
- `POST /v1/embed` with body `{ "directory_path": "/abs/path/to/repo" }`

Embeddings are written to `{directory_path}/.tursor/embeddings/manifest.json` and `chunks.jsonl`.

Re-running `/v1/embed` performs an **incremental update**: unchanged files reuse existing vectors; added/changed/deleted files are merged automatically.

Optional `include.patterns` in config (e.g. `["src/**"]`) limits which files are indexed. Without it, all supported text extensions under the workspace are indexed minus `excluded`.
