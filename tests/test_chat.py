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

    explain = client.post(
        "/v1/chat/completion",
        json={
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
    assert explained["cdp_steps"] is None

    res = client.post(
        "/v1/chat/completion",
        json={
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
    assert body["reply"]
    assert body["cdp_steps"]
    assert len(body["cdp_steps"]) >= 3
    assert body["cdp_steps"][0]["id"]
    assert body["cdp_steps"][0]["label"]
    assert body["cdp_steps"][0]["actions"]
