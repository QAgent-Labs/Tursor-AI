# Tursor-AI

FastAPI service on port **8000**. It indexes a workspace, searches those embeddings, and answers one chat turn. It does not store conversations and it does not run the browser.

## Setup

```bash
cd Tursor-AI
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Workspace config: `{workspace}/.tursor/config.json`

```json
{
  "excluded": ["node_modules", "dist", ".git"]
}
```

Embeddings use [fastembed](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5`). The first run downloads the model.

`TURSOR_AI_HASH_EMBEDDINGS=1` uses hash vectors for tests. `TURSOR_AI_MOCK_LLM=1` skips OpenAI and returns a fixed login plan when the message asks for CDP steps.

## Run

```bash
python run.py
```

## Flow

```text
Backend
  |  case, plans[{id,title}], latest plan steps, new user message
  v
POST /v1/chat/completion
  |  embed the question
  |  cosine search over .tursor/embeddings/chunks.jsonl
  |  one JSON call to the model
  v
{ "reply", "case", "cdp_steps" | null }
```

`cdp_steps` is null for a normal answer. The model asks if the user wants steps. It fills `cdp_steps` in that same response when the user agrees, or says to create the steps or start the test.

Each step is the shape the runner executes:

```json
{
  "id": "click-get-started",
  "label": "Click Get started",
  "actions": [
    { "type": "click", "selectors": ["[data-testid=\"get-started\"]"] }
  ]
}
```

Action types: `navigate`, `click`, `fill`, `waitForText`, `waitForPath`.

The backend saves that array as its own row and returns only `reply` and `cdpStepsId` to the extension.

## API

| Method | Path | Body |
|---|---|---|
| `GET` | `/health` | |
| `GET` | `/v1/validate?directory_path=` | |
| `POST` | `/v1/embed` | `{ "directory_path" }` |
| `POST` | `/v1/rag/search` | `{ "directory_path", "query", "top_k" }` |
| `POST` | `/v1/chat/completion` | see below |

### `POST /v1/chat/completion`

```json
{
  "workspace_path": "/abs/workspace",
  "message": "create the cdp steps for login",
  "generation_model": "gpt-5.6",
  "api_key": "sk-...",
  "mode": "chat",
  "case": "User wants to test login.",
  "plans": [{ "id": "uuid", "title": "Login" }],
  "latest_cdp_steps": null
}
```

`mode` is `intro` or `chat`. The response:

```json
{
  "reply": "Created the login steps.",
  "case": "User wants a login test: home, Get started, credentials, /done.",
  "cdp_steps": [],
  "retrieved_chunk_count": 8
}
```

`cdp_steps` is `null` when no plan was created.

Embeddings are written to `{workspace}/.tursor/embeddings/manifest.json` and `chunks.jsonl`. Re-running `/v1/embed` updates only files that changed.
