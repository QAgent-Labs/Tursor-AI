import pytest
from fastapi.testclient import TestClient

from app.embed_service import build_embeddings
from app.main import app
from app.settings import settings


@pytest.fixture(autouse=True)
def hash_mode() -> None:
    settings.hash_embeddings = True


def test_chat_completion_mock_llm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")

    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text(
        """
        {
          "ai": {
            "generationModel": "gpt-5.6",
            "apiKey": "sk-test"
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")

    build_embeddings(str(tmp_path))

    client = TestClient(app)

    conversation_id = "11111111-1111-4111-8111-111111111111"
    explain = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": conversation_id,
            "workspace_path": str(tmp_path),
            "message": "How does the login flow work?",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
            "mode": "chat",
        },
    )
    assert explain.status_code == 200
    explained = explain.json()
    assert explained["reply"]
    assert explained["conversation_id"] == conversation_id
    assert explained["response_id"]
    assert explained["test_suite"] is None

    missing = client.post(
        "/v1/chat/completion",
        json={
            "workspace_path": str(tmp_path),
            "message": "How does the login flow work?",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert missing.status_code == 422

    res = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": conversation_id,
            "workspace_path": str(tmp_path),
            "message": "okay create me the cdp steps for this flow please",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
            "mode": "chat",
            "case": "User wants to understand login.",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["conversation_id"] == conversation_id
    assert body["response_id"]
    assert body["response_id"] != explained["response_id"]
    suite = body["test_suite"]
    assert suite["feature"] == "Login"
    kinds = [case["kind"] for case in suite["cases"]]
    assert kinds[0] == "success"
    assert "failure" in kinds
    assert suite["cases"][0]["steps"]
    assert suite["cases"][0]["steps"][0]["actions"]
    assert body["intent"] == "test_generation"


def test_general_question_does_not_retrieve(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    build_embeddings(str(tmp_path))

    def fail_search(*_args, **_kwargs):
        raise AssertionError("general questions must not search the workspace")

    monkeypatch.setattr("app.chat_service.search_workspace", fail_search)
    client = TestClient(app)
    res = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "What is regression testing?",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "general_qa"
    assert body["test_suite"] is None
    assert body["retrieved_chunk_count"] == 0


def test_unnamed_feature_is_not_assumed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    build_embeddings(str(tmp_path))

    def fail_search(*_args, **_kwargs):
        raise AssertionError("an unnamed feature must not search the workspace")

    monkeypatch.setattr("app.chat_service.search_workspace", fail_search)
    client = TestClient(app)
    res = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "hi i need to know how to test a feature",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["test_suite"] is None
    assert body["retrieved_chunk_count"] == 0
    assert "Which feature" in body["reply"]

    greeting = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "hi",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
            "case": "No requirement has been given yet.",
        },
    )
    assert greeting.status_code == 200
    greeted = greeting.json()
    assert greeted["intent"] == "general_qa"
    assert greeted["retrieved_chunk_count"] == 0
    assert "Which feature" not in greeted["reply"]


def test_unmapped_flow_searches_code_without_nearest_features(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "RegisterPage.tsx").write_text(
        "export function RegisterPage() { return <h2>Create account</h2>; }\n",
        encoding="utf-8",
    )
    build_embeddings(str(tmp_path))

    def fail_select(*_args, **_kwargs):
        raise AssertionError("an unmatched flow must not attach the nearest map features")

    monkeypatch.setattr("app.chat_service.select_features", fail_select)
    client = TestClient(app)
    res = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "i want to test the account creation flow",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "test_generation"
    assert body["retrieved_chunk_count"] > 0
    assert body["test_suite"]["feature"] == "Login"

    explain = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "how does account creation work?",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert explain.status_code == 200
    explained = explain.json()
    assert explained["intent"] == "feature"
    assert explained["test_suite"] is None
    assert explained["retrieved_chunk_count"] > 0


def test_confirmation_reuses_saved_files(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    build_embeddings(str(tmp_path))

    def fail_search(*_args, **_kwargs):
        raise AssertionError("a confirmation must reuse the saved files")

    monkeypatch.setattr("app.chat_service.search_workspace", fail_search)
    client = TestClient(app)
    res = client.post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "yes",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
            "intent": "feature",
            "focus_paths": ["app.ts"],
            "case": "Login flow.",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "test_generation"
    assert body["test_suite"]["feature"] == "Login"
    assert body["focus_paths"] == ["app.ts"]
