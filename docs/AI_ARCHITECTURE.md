# Tursor AI Architecture

The current flow is in [`README.md`](../README.md). This file describes an older propose-then-generate-Playwright path and is not the implementation.

---

This document describes the **implemented** AI layer (Backend REST + Tursor-AI), how prompts work, and how to test without the Extension.

> **Pin-to-pin code guide:** [`AI_IMPLEMENTATION.md`](./AI_IMPLEMENTATION.md)  
> **Supabase setup:** [`../Tursor-Backend/docs/SUPABASE_SETUP.md`](../Tursor-Backend/docs/SUPABASE_SETUP.md)

> **Extension is not wired yet.** Test via `curl` against Backend `:9090` and Tursor-AI `:8000`.

---

## 1. Layered responsibility

```text
Extension (future)
    │  REST only for chat
    ▼
Tursor Backend :9090
    ├── ChatOrchestratorService   conversation lifecycle + state machine
    ├── SupabaseChatService       Postgres persistence
    ├── WorkspaceConfigValidator reads ai.* + supabase.* from config.json
    └── TursorAiClient            HTTP to Tursor-AI
            ▼
Tursor-AI :8000
    ├── POST /v1/rag/search       cosine similarity over chunks.jsonl
    └── POST /v1/chat/completion  RAG + OpenAI-compatible LLM
            ▼
OpenAI API (or TURSOR_AI_MOCK_LLM=1 for offline tests)
```

**Execution stays on Backend WebSocket** — chat APIs never run Playwright directly from Tursor-AI.

---

## 2. Workspace config (`ai` block)

```json
{
  "ai": {
    "generationModel": "gpt-5.6",
    "apiKey": "sk-..."
  }
}
```

- Read by **Backend** from `{workspace}/.tursor/config.json`
- Passed to Tursor-AI on each `/v1/chat/completion` request
- **Never returned** in validate/chat API responses to clients

---

## 3. Tursor-AI endpoints

### `POST /v1/rag/search`

Semantic search over existing embeddings.

```json
{
  "directory_path": "/abs/workspace",
  "query": "how does checkout auth work",
  "top_k": 8
}
```

Returns ranked chunks `{ path, content, start_line, end_line, score }`.

**Algorithm:** embed query with same model as indexing → cosine similarity vs all rows in `chunks.jsonl`.

### `POST /v1/chat/completion`

Single entry point for intro, chat, and test generation.

```json
{
  "workspace_path": "/abs/workspace",
  "message": "Test the login flow",
  "generation_model": "gpt-5.6",
  "api_key": "sk-...",
  "mode": "chat",
  "conversation_state": "NORMAL",
  "conversation_summary": "",
  "recent_messages": [{ "role": "user", "content": "..." }],
  "approved_test_flow": null,
  "rag_top_k": 8
}
```

**Modes:**

| mode | RAG | Purpose |
|------|-----|---------|
| `intro` | optional | Welcome message on Run page |
| `chat` | yes | Q&A + test proposals |
| `generate_test` | yes | Playwright code after flow approval |

**Response types (structured JSON from LLM):**

| type | When |
|------|------|
| `conversation` | Normal Q&A |
| `test_proposal` | User describes something to test |
| `test_generation` | After approved flow (`generate_test` mode) |
| `error` | Parse/API failure |

---

## 4. System prompt (`app/prompts.py`)

The LLM receives a **fixed system prompt** plus a **dynamic user payload**.

### System prompt rules

1. Answer codebase questions using retrieved context; cite paths.
2. Detect testing intent → return `test_proposal`, **not** code.
3. Generate Playwright **only** when `mode=generate_test` or state is `AWAITING_TEST_APPROVAL`.
4. Never claim tests were executed.
5. Respond with **one JSON object** (no markdown fences).

### Mock CDP reference (embedded in prompt)

The prompt includes the **same 7-step Atelier Canvas demo** used by `Tursor-Backend` `demoCdpSteps()`:

1. `navigate-home` → `/`
2. `click-get-started`
3. `fill-credentials` (demo-user / demo-pass)
4. `submit-form`
5. `assert-done-hub` → `/done`
6. `open-category`
7. `assert-browse` → `/browse/`

Action types documented: `navigate`, `click`, `fill`, `waitForText`, `waitForPath`.

The model uses this as a **style reference** for `testFlow` steps and generated Playwright code.

### User payload (built per request)

```text
mode: chat
conversation_state: AWAITING_TEST_APPROVAL
conversation_summary: ...
recent_messages:
  [user]: ...
  [assistant]: ...
retrieved_workspace_context:
--- src/checkout.tsx:1-45 ---
<file chunk text>
approved_test_flow:   # generate_test mode only
user_message:
Test the checkout flow
```

---

## 5. Backend REST chat APIs

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/chat/intro` | Create conversation + intro message |
| `POST` | `/chat/message` | User message → RAG → LLM → persist |
| `POST` | `/chat/approve-test-flow` | Generate Playwright from last proposal |
| `POST` | `/chat/approve-execution` | Start CDP run (demo flow phase 1) |
| `GET` | `/chat/conversations/:id` | Debug: conversation + messages + test |

### Conversation state machine

```text
NORMAL
  → test_proposal from AI
AWAITING_TEST_APPROVAL
  → POST /chat/approve-test-flow
GENERATING_TEST → TEST_GENERATED
  → POST /chat/approve-execution
EXECUTING → COMPLETED (future)
```

### Supabase schema

Run `Tursor-Backend/scripts/supabase-chat-schema.sql` in your Supabase SQL editor (see `Tursor-Backend/docs/SUPABASE_SETUP.md`).

Uses `supabase.database.serviceRoleKey` from workspace config.

---

## 6. End-to-end test flow (curl)

**Prerequisites:**

1. `tursor start` (Backend :9090)
2. `tursorAI start` (AI :8000)
3. Supabase schema applied
4. `.tursor/config.json` with `supabase` + `ai`
5. Embeddings built (`start_context` or `POST /v1/embed`)
6. Set active workspace via WebSocket `workspace_init` **or** pass matching path

**Mock LLM (no OpenAI key):**

```bash
export TURSOR_AI_MOCK_LLM=1
# restart tursorAI after setting env
```

### Intro

```bash
curl -s -X POST http://127.0.0.1:9090/chat/intro \
  -H 'Content-Type: application/json' \
  -d '{"workspacePath":"/path/to/Trial-React-project"}' | jq
```

### Message

```bash
curl -s -X POST http://127.0.0.1:9090/chat/message \
  -H 'Content-Type: application/json' \
  -d '{
    "conversationId": "<uuid-from-intro>",
    "message": "Test the login flow",
    "workspacePath": "/path/to/Trial-React-project"
  }' | jq
```

Expect `ai.type: test_proposal` with `testFlow` array.

### Approve flow → generate test

```bash
curl -s -X POST http://127.0.0.1:9090/chat/approve-test-flow \
  -H 'Content-Type: application/json' \
  -d '{"conversationId":"<uuid>"}' | jq
```

Expect `generatedTest.code` with Playwright TypeScript.

### Approve execution

```bash
curl -s -X POST http://127.0.0.1:9090/chat/approve-execution \
  -H 'Content-Type: application/json' \
  -d '{"conversationId":"<uuid>"}'
```

Triggers existing **demo CDP run** via WebSocket (generated code persisted for phase 2 dynamic runner).

---

## 7. Mock vs real LLM

| Env | Behavior |
|-----|----------|
| `TURSOR_AI_MOCK_LLM=1` | Deterministic mock responses; test keywords → `test_proposal` |
| Real `ai.apiKey` | OpenAI `POST /v1/chat/completions` with `response_format: json_object` |

---

## 8. What is NOT implemented yet

- Extension REST client (UI still uses WebSocket chat stub)
- Dynamic execution of AI-generated Playwright (runs demo flow today)
- Conversation summarization for very long threads (schema supports `summary` field)
- Redis caching

---

## 9. File map

| File | Role |
|------|------|
| `Tursor-AI/app/prompts.py` | System prompt + mock CDP reference |
| `Tursor-AI/app/rag_service.py` | Semantic search |
| `Tursor-AI/app/llm_service.py` | OpenAI client + mock |
| `Tursor-AI/app/chat_service.py` | RAG + LLM orchestration |
| `Tursor-Backend/src/chat/chat-orchestrator.service.ts` | REST chat lifecycle |
| `Tursor-Backend/src/chat/supabase-chat.service.ts` | Postgres persistence |
| `Tursor-Backend/scripts/supabase-chat-schema.sql` | DB schema |
