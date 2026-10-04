# Tursor-AI — Complete Implementation Guide

The current flow is in [`README.md`](../README.md). Sections below that mention `test_proposal`, `generate_test`, or full message history are out of date.

---

Pin-to-pin reference for the **Tursor-AI** service: every module, endpoint, prompt, and data flow.

> **Yes — Tursor-AI is built on FastAPI.**  
> Auto-generated OpenAPI docs: **http://localhost:8000/docs** (Swagger UI) and **http://localhost:8000/redoc**.

> **Backend (NestJS)** exposes its own Swagger at **http://localhost:9090/api**.

Related docs:

- High-level architecture: [`AI_ARCHITECTURE.md`](./AI_ARCHITECTURE.md)
- Supabase setup: [`../Tursor-Backend/docs/SUPABASE_SETUP.md`](../Tursor-Backend/docs/SUPABASE_SETUP.md)

---

## 1. What Tursor-AI does

Tursor-AI is a **stateless Python microservice** that:

1. **Indexes** workspace source files into embeddings (`POST /v1/embed`)
2. **Searches** those embeddings semantically (`POST /v1/rag/search`)
3. **Completes chat** with RAG + LLM (`POST /v1/chat/completion`)

It does **not**:

- Store conversations (Backend + Supabase do that)
- Execute Playwright tests (Backend CDP runner does that)
- Talk to the Extension directly (Extension → Backend → Tursor-AI)

---

## 2. Tech stack

| Layer         | Choice                                              |
| ------------- | --------------------------------------------------- |
| Web framework | **FastAPI** 0.1.x                                   |
| Validation    | **Pydantic** v2 (`app/schemas.py`)                  |
| Embeddings    | **fastembed** (ONNX) or hash fallback for tests     |
| LLM           | OpenAI-compatible `POST /v1/chat/completions`       |
| Storage       | Local files under `{workspace}/.tursor/embeddings/` |
| Config        | `{workspace}/.tursor/config.json` (`ai` block)      |

Entry point: `run.py` → uvicorn → `app.main:app`

---

## 3. Directory map

```text
Tursor-AI/
├── app/
│   ├── main.py           # FastAPI routes
│   ├── schemas.py        # Request/response Pydantic models
│   ├── settings.py       # Port, embed model, hash mode
│   ├── tursor_config.py  # Load .tursor/config.json
│   ├── file_walker.py    # Walk workspace files
│   ├── chunking.py       # Split files into text chunks
│   ├── embedder.py       # fastembed / hash vectors
│   ├── storage.py        # manifest.json + chunks.jsonl
│   ├── embed_service.py  # Full embed pipeline
│   ├── rag_service.py    # Cosine similarity search
│   ├── prompts.py        # SYSTEM_PROMPT + mock CDP reference
│   ├── llm_service.py    # OpenAI client + mock LLM
│   └── chat_service.py   # RAG + LLM orchestration
├── tests/
│   ├── test_embed.py
│   ├── test_rag.py
│   └── test_chat.py
├── docs/
│   ├── AI_ARCHITECTURE.md
│   └── AI_IMPLEMENTATION.md   ← this file
└── run.py
```

---

## 4. Configuration (`app/tursor_config.py`)

### Workspace config file

Path: `{workspace_root}/.tursor/config.json`

Relevant fields for Tursor-AI:

```json
{
  "include": { "patterns": ["src/**"] },
  "excluded": ["node_modules"],
  "ai": {
    "generationModel": "gpt-5.6",
    "apiKey": "sk-..."
  }
}
```

### `TursorWorkspaceConfig` dataclass

| Field              | Source                                  | Purpose                     |
| ------------------ | --------------------------------------- | --------------------------- |
| `workspace_root`   | API `directory_path` / `workspace_path` | Absolute path               |
| `excluded`         | `excluded[]`                            | Directory names to skip     |
| `include_patterns` | `include.patterns[]`                    | fnmatch globs               |
| `ai`               | `ai` block                              | Optional; required for chat |

Properties:

- `config_path` → `.tursor/config.json`
- `embeddings_path` → `.tursor/embeddings/`

### `load_tursor_config(workspace_root)`

1. Resolve path, verify directory exists
2. Parse JSON, validate types
3. Return frozen dataclass

Raises `TursorConfigError` on missing/invalid config (HTTP 400).

**Note:** Supabase `bucket` / `database` blocks are parsed by **Backend only**. Tursor-AI reads `ai` when validating chat-related workspace config.

---

## 5. Settings (`app/settings.py`)

Environment-driven defaults:

| Setting                 | Env var                     | Default                  |
| ----------------------- | --------------------------- | ------------------------ |
| Port                    | `TURSOR_AI_PORT`            | `8000`                   |
| Embed model             | `TURSOR_AI_EMBED_MODEL`     | `BAAI/bge-small-en-v1.5` |
| Hash embeddings (tests) | `TURSOR_AI_HASH_EMBEDDINGS` | off                      |

Tests set `settings.hash_embeddings = True` for fast deterministic vectors.

---

## 6. Embedding pipeline

### 6.1 File discovery (`app/file_walker.py`)

`iter_indexable_files(cfg)` walks `workspace_root`, skips:

- Hidden paths, `.tursor/`, `node_modules`, etc.
- Paths in `excluded`
- Files not matching `include.patterns` (if set)

Only text-like extensions are indexed (`.ts`, `.tsx`, `.py`, `.json`, etc.).

### 6.2 Chunking (`app/chunking.py`)

`chunk_file(path, workspace_root)`:

- Reads file text
- Splits into `TextChunk` records with:
  - `relative_path`, `chunk_index`
  - `start_line`, `end_line`
  - `text` (chunk body)

Chunk size tuned for RAG retrieval (~lines per chunk).

### 6.3 Embedding (`app/embedder.py`)

`embed_texts(texts: list[str]) → list[list[float]]`

**Production mode:** loads `fastembed.TextEmbedding` once (cached), batches text.

**Test/hash mode:** `_hash_embed_text()` — SHA256-seeded pseudo-vectors, L2-normalized. Same input → same vector (deterministic tests).

`model_name()` returns either `hash-{dim}` or the fastembed model id (stored in manifest).

### 6.4 Persistence (`app/storage.py`)

Written under `.tursor/embeddings/`:

**`manifest.json`** — metadata:

```json
{
  "version": 2,
  "workspace_root": "/abs/path",
  "model": "BAAI/bge-small-en-v1.5",
  "files_indexed": 42,
  "chunks_indexed": 180,
  "files": { "src/App.tsx": { "mtime_ns", "size", "content_hash" } }
}
```

**`chunks.jsonl`** — one JSON object per line:

```json
{"path":"src/App.tsx","chunk_index":0,"start_line":1,"end_line":45,"text":"...","embedding":[0.01,...]}
```

Incremental re-embed: `embed_service.py` compares file fingerprints in manifest; only changed files are re-chunked.

### 6.5 Embed service (`app/embed_service.py`)

`build_embeddings(directory_path)`:

1. `load_tursor_config`
2. Walk files, chunk, embed
3. `write_embeddings(...)`
4. Return `EmbedResponse` with counts

`validate_workspace(directory_path)` — config-only check for `GET /v1/validate`.

---

## 7. RAG (`app/rag_service.py`)

### Algorithm

```text
query string
  → embed_texts([query])           # same model as indexing
  → load_stored_chunks(embeddings/)
  → for each chunk: cosine_sim(query_vec, chunk.embedding)
  → sort descending, take top_k
  → return [{ path, content, start_line, end_line, score }]
```

### `search_workspace(workspace_path, query, top_k=8)`

- Loads config + chunks
- Empty index → `[]`
- Scores rounded to 4 decimal places

### `ensure_embeddings_exist(cfg)`

Called before chat RAG; raises `ValueError` if `chunks.jsonl` missing → HTTP 400 with "Run POST /v1/embed first."

---

## 8. Prompts (`app/prompts.py`)

### 8.1 Mock CDP reference (`MOCK_CDP_REFERENCE`)

Embedded in the system prompt. **Same 7-step flow** as Backend `demoCdpSteps()` (Atelier Canvas):

| Step ID             | Action                             |
| ------------------- | ---------------------------------- |
| `navigate-home`     | `navigate` → `/`                   |
| `click-get-started` | `click` with testid/text selectors |
| `fill-credentials`  | `fill` username + password         |
| `submit-form`       | `click` submit                     |
| `assert-done-hub`   | `waitForPath` `/done`              |
| `open-category`     | `click` browse link                |
| `assert-browse`     | `waitForPath` `/browse/`           |

Action types documented: `navigate`, `click`, `fill`, `waitForText`, `waitForPath`.

The LLM uses this JSON as a **style template** for `test_proposal.testFlow` and generated Playwright code.

### 8.2 System prompt (`SYSTEM_PROMPT`)

Rules enforced in prompt text:

1. Answer codebase questions using retrieved context; cite paths
2. Testing intent → return `test_proposal`, not code
3. Playwright code **only** when `mode=generate_test` or state `AWAITING_TEST_APPROVAL`
4. Never claim tests were executed
5. Response = **single JSON object**, no markdown fences

### 8.3 Response JSON shapes (enforced by prompt)

**Normal chat:**

```json
{ "type": "conversation", "content": "..." }
```

**Test proposal:**

```json
{
  "type": "test_proposal",
  "content": "Summary asking for approval",
  "status": "approval_required",
  "testFlow": [{ "step": 1, "action": "..." }]
}
```

**Generated test:**

```json
{
  "type": "test_generation",
  "content": "...",
  "status": "generated",
  "language": "typescript",
  "framework": "playwright",
  "testName": "name",
  "code": "full source string"
}
```

**Error:**

```json
{ "type": "error", "content": "..." }
```

### 8.4 Intro prompt (`INTRO_USER_PROMPT`)

Short instruction for welcome message on Run page (< 3 sentences).

---

## 9. LLM service (`app/llm_service.py`)

### Mock mode

Set `TURSOR_AI_MOCK_LLM=1` (or `true` / `yes`):

| `mode`          | Trigger                                 | Response                               |
| --------------- | --------------------------------------- | -------------------------------------- |
| `intro`         | always                                  | `conversation` welcome text            |
| `generate_test` | always                                  | `test_generation` with mock Playwright |
| `chat`          | message contains test/verify/playwright | `test_proposal` with 5 steps           |
| `chat`          | otherwise                               | generic `conversation`                 |

### Real OpenAI mode

`complete_chat(...)`:

1. `_build_user_payload(...)` — structured context (see below)
2. HTTP POST `{openai_base_url}/chat/completions`:

```json
{
  "model": "gpt-5.6",
  "messages": [
    { "role": "system", "content": "<SYSTEM_PROMPT>" },
    { "role": "user", "content": "<dynamic payload>" }
  ],
  "temperature": 1,
  "response_format": { "type": "json_object" }
}
```

3. `_parse_json_response(content)` — strip accidental ```fences, parse JSON, require`type` field

Raises `LlmError` on HTTP errors or invalid JSON → chat_service returns `{type:"error", content:"..."}`.

### Dynamic user payload structure

Built by `_build_user_payload`:

```text
mode: chat
conversation_state: AWAITING_TEST_APPROVAL
conversation_summary:
<optional summary>

recent_messages:
  [user]: ...
  [assistant]: ...

retrieved_workspace_context:
--- src/App.tsx:1-45 ---
<chunk text>

approved_test_flow:
[ ... ]                    # generate_test mode only

user_message:
Test the login flow
```

For `mode=intro`, `INTRO_USER_PROMPT` replaces `user_message`.

---

## 10. Chat orchestration (`app/chat_service.py`)

### `run_chat_completion(...)`

Single function called by `POST /v1/chat/completion`:

```python
async def run_chat_completion(
    workspace_path,
    message,
    generation_model,      # from Backend (config ai.generationModel)
    api_key,               # from Backend (config ai.apiKey) — never logged
    mode="chat",           # intro | chat | generate_test
    conversation_state="NORMAL",
    conversation_summary=None,
    recent_messages=None,
    approved_test_flow=None,
    rag_query=None,
    rag_top_k=8,
) -> dict
```

**RAG decision:**

```python
if mode != "generate_test" or rag_query:
    ensure_embeddings_exist(cfg)
    query = rag_query or message
    retrieved = search_workspace(workspace_path, query, top_k=rag_top_k)
```

- `chat` / `intro` → always RAG on user message
- `generate_test` → RAG only if `rag_query` set (Backend sends it)

**LLM call:**

```python
ai_response = await complete_chat(
    generation_model=...,
    api_key=...,
    mode=mode,
    message=message,
    conversation_state=conversation_state,
    conversation_summary=conversation_summary,
    recent_messages=recent_messages,
    retrieved_context=retrieved,
    approved_test_flow=approved_test_flow,
)
```

**Return:** spread `ai_response` + `retrieved_chunk_count`.

---

## 11. HTTP API (`app/main.py`)

### `GET /health`

Returns `{ status: "ok", service: "tursor-ai", port: 8000 }`.

### `GET /v1/validate?directory_path=...`

Validates config exists; returns paths, include patterns, `ai_configured`.

### `POST /v1/embed`

Body: `{ "directory_path": "/abs/workspace" }`

Runs full embed pipeline; returns chunk/file counts.

### `POST /v1/rag/search`

Body:

```json
{
  "directory_path": "/abs/workspace",
  "query": "how does auth work",
  "top_k": 8
}
```

Response: ranked `chunks[]` with scores.

### `POST /v1/chat/completion`

Body (see `ChatCompletionRequest` in `schemas.py`):

| Field                  | Required | Notes                                |
| ---------------------- | -------- | ------------------------------------ |
| `workspace_path`       | Yes      | Absolute workspace root              |
| `message`              | Yes      | User text or "intro"                 |
| `generation_model`     | Yes      | Passed by Backend from config        |
| `api_key`              | Yes      | Passed by Backend from config        |
| `mode`                 | No       | `chat` \| `intro` \| `generate_test` |
| `conversation_state`   | No       | Backend state machine value          |
| `conversation_summary` | No       | Future summarization                 |
| `recent_messages`      | No       | Last N messages from Supabase        |
| `approved_test_flow`   | No       | For `generate_test`                  |
| `rag_query`            | No       | Override RAG query                   |
| `rag_top_k`            | No       | Default 8                            |

Response (`ChatCompletionResponse`): `type`, `content`, `testFlow`, `code`, etc.

---

## 12. Backend integration (how Tursor-AI is called)

Backend never exposes `api_key` to clients. Flow:

```text
POST /chat/message (NestJS)
  → ChatOrchestratorService.loadWorkspaceBundle()
      reads .tursor/config.json → ai.generationModel, ai.apiKey
  → TursorAiClient.chatCompletion({
        workspace_path,
        message,
        generation_model: bundle.ai.generationModel,
        api_key: bundle.ai.apiKey,
        mode: "chat",
        conversation_state,
        recent_messages,
     })
  → HTTP POST http://127.0.0.1:8000/v1/chat/completion
```

Same pattern for `intro` and `approve-test-flow` (`mode: "generate_test"`).

---

## 13. End-to-end chat sequence

```text
1. POST /chat/intro
   Backend: createConversation (Supabase database config)
   Backend → AI: mode=intro
   AI → intro message
   Backend: insert assistant message

2. POST /chat/message  "Test login flow"
   Backend: insert user message
   Backend → AI: mode=chat, RAG on message
   AI → test_proposal JSON
   Backend: status → AWAITING_TEST_APPROVAL

3. POST /chat/approve-test-flow
   Backend: extract testFlow from last proposal
   Backend → AI: mode=generate_test, approved_test_flow
   AI → test_generation + Playwright code
   Backend: save generated_tests row

4. POST /chat/approve-execution
   Backend: status → EXECUTING
   Backend: startCdpRun() (demo flow phase 1)
```

---

## 14. Testing

### Run all tests

```bash
cd Tursor-AI
PYTHONPATH=. ~/.tursor-ai/.venv/bin/pytest tests/ -q
```

### `tests/test_rag.py`

Creates temp workspace, embeds file, searches — verifies ranking.

### `tests/test_chat.py`

- Sets `TURSOR_AI_MOCK_LLM=1`
- Embeds minimal project
- POST `/v1/chat/completion` with "test login"
- Asserts `type == test_proposal` and `testFlow` length ≥ 3

### Manual Swagger testing

1. `tursorAI start`
2. Open http://localhost:8000/docs
3. Try `POST /v1/embed` then `POST /v1/rag/search`

---

## 15. Swagger / OpenAPI

| Service            | URL                         | Framework                |
| ------------------ | --------------------------- | ------------------------ |
| **Tursor-AI**      | http://localhost:8000/docs  | FastAPI (built-in)       |
| **Tursor-AI**      | http://localhost:8000/redoc | FastAPI ReDoc            |
| **Tursor Backend** | http://localhost:9090/api   | NestJS `@nestjs/swagger` |

FastAPI generates schemas from Pydantic models in `app/schemas.py` automatically.

---

## 16. Environment variables summary

| Variable                      | Service   | Purpose                           |
| ----------------------------- | --------- | --------------------------------- |
| `TURSOR_AI_MOCK_LLM=1`        | Tursor-AI | Offline LLM for dev/tests         |
| `TURSOR_AI_PORT`              | Tursor-AI | Listen port (default 8000)        |
| `TURSOR_AI_HASH_EMBEDDINGS=1` | Tursor-AI | Hash vectors instead of fastembed |
| `TURSOR_AI_URL`               | Backend   | AI base URL (default :8000)       |

Workspace secrets (`ai.apiKey`, `supabase.*.serviceRoleKey`) live in **`.tursor/config.json` only**.

---

## 17. Code reading order (recommended)

For a new developer, read in this order:

1. `app/tursor_config.py` — how workspace config loads
2. `app/embed_service.py` + `app/storage.py` — indexing
3. `app/rag_service.py` — retrieval
4. `app/prompts.py` — what the LLM is told
5. `app/llm_service.py` — how requests are sent/parsed
6. `app/chat_service.py` — ties RAG + LLM
7. `app/main.py` — HTTP surface
8. `Tursor-Backend/src/chat/chat-orchestrator.service.ts` — lifecycle + persistence

---

## 18. Known limitations (phase 1)

- No conversation storage in Tursor-AI (by design)
- No dynamic Playwright execution from generated code
- No automatic conversation summarization
- Extension not wired to REST chat yet

---

## 19. Quick reference — file → responsibility

| File               | One-line summary              |
| ------------------ | ----------------------------- |
| `main.py`          | FastAPI routes + Swagger      |
| `schemas.py`       | Pydantic IO models            |
| `tursor_config.py` | Parse `.tursor/config.json`   |
| `file_walker.py`   | Find indexable source files   |
| `chunking.py`      | Split files into chunks       |
| `embedder.py`      | Text → vectors                |
| `storage.py`       | Read/write chunks.jsonl       |
| `embed_service.py` | Full index pipeline           |
| `rag_service.py`   | Cosine search                 |
| `prompts.py`       | System prompt + CDP reference |
| `llm_service.py`   | OpenAI + mock                 |
| `chat_service.py`  | RAG + LLM entry               |
