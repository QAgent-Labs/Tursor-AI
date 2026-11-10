import pytest
from fastapi.testclient import TestClient

from app.embed_service import build_embeddings
from app.main import app
from app.settings import settings

CONVERSATION = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings.hash_embeddings = True
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")


@pytest.fixture()
def workspace(tmp_path):
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text(
        '{"ai": {"generationModel": "gpt-5.6", "apiKey": "sk-test"}}', encoding="utf-8"
    )
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    build_embeddings(str(tmp_path))
    return tmp_path


def _chat(workspace, **fields):
    body = {
        "conversation_id": CONVERSATION,
        "workspace_path": str(workspace),
        "generation_model": "gpt-5.6",
        "api_key": "sk-test",
        **fields,
    }
    res = TestClient(app).post("/v1/chat/completion", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_asking_about_a_flow_explains_and_asks_first(workspace) -> None:
    body = _chat(workspace, message="i wanna test the login flow")
    assert body["intent"] == "feature"
    assert body["test_suite"] is None
    assert body["pending_action"] == "generate"
    assert "### How it works" in body["reply"]


def test_yes_to_the_question_generates(workspace) -> None:
    body = _chat(
        workspace,
        message="yes",
        case="Login flow.",
        pending_action="generate",
    )
    assert body["intent"] == "test_generation"
    suite = body["test_suite"]
    assert suite["feature"] == "Login"
    assert [case["kind"] for case in suite["cases"]][0] == "success"
    assert body["pending_action"] == ""


def test_yes_without_a_question_does_not_generate(workspace) -> None:
    body = _chat(workspace, message="yes", case="Login flow.")
    assert body["test_suite"] is None


def test_values_still_missing_block_a_yes(workspace, monkeypatch) -> None:
    async def not_ready(**_kwargs):
        return {
            "reply": "### What I still need\n- a valid password",
            "case": "Login flow; password missing.",
            "brief_summary": "Login",
            "test_suite": None,
            "ready": False,
        }

    monkeypatch.setattr("app.chat_service.complete_chat", not_ready)
    body = _chat(workspace, message="explain the login flow please")
    assert body["intent"] == "feature"
    assert body["pending_action"] == "collect_generate"
    # A plain "yes" to a request for values is not enough to generate.
    still = _chat(
        workspace, message="yes", pending_action="collect_generate", case=body["case"]
    )
    assert still["test_suite"] is None
    assert still["pending_action"] == "collect_generate"


def test_supplying_the_values_generates_in_the_same_turn(workspace) -> None:
    body = _chat(
        workspace,
        message="use any of the demo accounts",
        pending_action="collect_generate",
        case="Login flow; a valid email and password are still needed.",
    )
    assert body["intent"] == "test_generation"
    assert body["test_suite"]["feature"] == "Login"
    assert body["pending_action"] == ""
    assert len(body["suite_fingerprint"]) == 64


def test_general_question_does_not_retrieve(workspace, monkeypatch) -> None:
    def fail_search(*_args, **_kwargs):
        raise AssertionError("general questions must not search the workspace")

    monkeypatch.setattr("app.chat_service.search_workspace", fail_search)
    for message in (
        "What is regression testing?",
        "whats smoke test",
        "difference between smoke and sanity testing",
    ):
        body = _chat(workspace, message=message)
        assert body["intent"] == "general_qa", message
        assert body["retrieved_chunk_count"] == 0


def test_unnamed_feature_is_not_assumed(workspace) -> None:
    body = _chat(workspace, message="hi i need to know how to test a feature")
    assert body["test_suite"] is None
    assert "Which feature" in body["reply"]


def test_declining_the_question_generates_nothing(workspace) -> None:
    body = _chat(workspace, message="no", pending_action="generate", case="Login flow.")
    assert body["intent"] == "declined"
    assert body["test_suite"] is None
    assert body["pending_action"] == ""


def test_a_change_to_the_working_suite_generates(workspace) -> None:
    body = _chat(
        workspace,
        message="generate the test for an empty email too",
        active_suite_id="22222222-2222-4222-8222-222222222222",
        latest_test_suite={
            "suite_id": "22222222-2222-4222-8222-222222222222",
            "module_key": "/login",
            "version": 1,
            "feature": "Login",
            "cases": [],
        },
    )
    assert body["intent"] == "test_generation"
    assert body["test_suite"]["module_key"] == "/login"


SAVED = {
    "suite_id": "s1",
    "module_key": "/login",
    "module_name": "Sign in",
    "version": 2,
    "cases": [
        {
            "id": "c1",
            "title": "Valid sign-in",
            "kind": "success",
            "last_status": "passed",
            "last_run": {"run_id": "r1", "status": "passed"},
        },
        {
            "id": "c2",
            "title": "Redirect back to Orders",
            "kind": "success",
            "last_status": "failure",
            "last_run": {
                "run_id": "r2",
                "status": "failure",
                "failed_step": "Wait for the Orders page",
                "status_message": "waitForPath timed out",
            },
        },
    ],
}


def test_a_question_about_runs_is_answered_from_the_run_data(workspace) -> None:
    body = _chat(
        workspace,
        message="did any run fail?",
        existing_suites=[SAVED],
        pending_action="new_version",
        pending_suite_id="s1",
    )
    assert body["intent"] == "suite_status"
    assert body["test_suite"] is None
    assert "1 passed, 1 failed, 0 not run" in body["reply"]
    assert "**Redirect back to Orders** failed at Wait for the Orders page" in body["reply"]
    assert "`r2`" in body["reply"]
    # The question in between keeps the question Tursor asked before.
    assert body["pending_action"] == "new_version"
    assert body["pending_suite_id"] == "s1"


def test_a_prd_is_checked_even_while_a_question_is_pending(workspace) -> None:
    body = _chat(
        workspace,
        message="run login flow against this please\n\nPRD: Sign in\n"
        "R1. Users sign in with email and password.",
        existing_suites=[SAVED],
        pending_action="new_version",
        pending_suite_id="s1",
    )
    assert body["test_suite"] is None
    assert body["prd"] is not None
    # The spec check asks before generating anything.
    assert body["pending_action"] == "generate"


def test_the_saved_suite_line_is_computed_not_written(workspace) -> None:
    body = _chat(
        workspace, message="i wanna test the sign in flow", existing_suites=[SAVED]
    )
    assert body["reply"].startswith(
        "This feature already has a test suite: **Sign in v2** with 2 cases "
        "(2 success, 0 failure, 0 edge). Latest runs: 1 passed, 1 failed."
    )
