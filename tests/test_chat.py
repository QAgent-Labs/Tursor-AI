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
