import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.embed_service import build_embeddings
from app.main import app
from app.settings import settings

CONVERSATION = "11111111-1111-4111-8111-111111111111"
LOGIN_SUITE = {
    "suite_id": "22222222-2222-4222-8222-222222222222",
    "module_key": "/login",
    "module_name": "Login",
    "feature": "Login",
    "version": 3,
    "created_at": "2026-10-20T10:00:00Z",
    "cases": [
        {"title": "Valid sign-in reaches the studio", "kind": "success", "last_status": "passed"},
        {"title": "Wrong password shows an error", "kind": "failure", "last_status": ""},
    ],
}


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings.hash_embeddings = True
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")


@pytest.fixture()
def workspace(tmp_path):
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "LoginPage.tsx").write_text(
        "export function LoginPage() { return <h2>Sign in</h2>; }\n", encoding="utf-8"
    )
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


def _suite(**overrides):
    return {**LOGIN_SUITE, **overrides}


def test_a_valid_saved_suite_is_offered_for_reuse(workspace) -> None:
    body = _chat(
        workspace,
        message="i wanna test the login flow",
        existing_suites=[_suite()],
    )
    assert body["intent"] == "feature"
    assert body["test_suite"] is None
    assert body["pending_action"] == "reuse"
    assert body["pending_suite_id"] == LOGIN_SUITE["suite_id"]
    check = body["suite_check"]
    assert check["valid"] is True
    assert check["fingerprint"]


def test_a_stale_saved_suite_offers_the_next_version(workspace) -> None:
    stale = _suite(
        cases=[
            {
                "id": "c1",
                "title": "Valid sign-in reaches the studio",
                "kind": "success",
                "steps": [
                    {
                        "id": "wait",
                        "label": "Wait for the welcome",
                        "actions": [{"type": "waitForText", "text": "Welcome back, curator"}],
                    }
                ],
            }
        ]
    )
    body = _chat(workspace, message="i wanna test the login flow", existing_suites=[stale])
    assert body["pending_action"] == "new_version"
    check = body["suite_check"]
    assert check["valid"] is False
    assert check["stale_cases"][0]["title"] == "Valid sign-in reaches the studio"
    assert "Welcome back, curator" in check["stale_cases"][0]["reason"]


def test_an_unchanged_flow_reuses_the_stored_verdict(workspace) -> None:
    first = _chat(workspace, message="test the login flow", existing_suites=[_suite()])
    stored = _suite(
        checked_fingerprint=first["suite_check"]["fingerprint"],
        check_result={"valid": False, "stale_cases": [{"title": "x", "reason": "y"}]},
    )
    second = _chat(workspace, message="test the login flow", existing_suites=[stored])
    assert second["suite_check"]["reused"] is True
    assert second["pending_action"] == "new_version"


def test_yes_to_reuse_returns_the_saved_suite(workspace) -> None:
    body = _chat(
        workspace,
        message="yes",
        pending_action="reuse",
        pending_suite_id=LOGIN_SUITE["suite_id"],
        existing_suites=[_suite()],
    )
    assert body["intent"] == "suite_reused"
    assert body["reuse_suite_id"] == LOGIN_SUITE["suite_id"]
    assert body["test_suite"] is None


def test_no_to_reuse_asks_about_the_next_version(workspace) -> None:
    body = _chat(
        workspace,
        message="no",
        pending_action="reuse",
        pending_suite_id=LOGIN_SUITE["suite_id"],
        existing_suites=[_suite()],
    )
    assert body["intent"] == "declined"
    assert body["pending_action"] == "new_version"
    assert "v4" in body["reply"]


def test_yes_to_the_next_version_generates_from_the_base(workspace) -> None:
    body = _chat(
        workspace,
        message="yes",
        pending_action="new_version",
        pending_suite_id=LOGIN_SUITE["suite_id"],
        existing_suites=[_suite()],
        latest_test_suite={
            "suite_id": LOGIN_SUITE["suite_id"],
            "module_key": "/login",
            "version": 3,
            "feature": "Login",
            "cases": [],
        },
    )
    assert body["intent"] == "test_generation"
    assert body["test_suite"]["module_key"] == "/login"


def test_a_pasted_prd_gets_a_spec_check(workspace) -> None:
    prd = (
        "PRD: Sign in\n"
        "- Users sign in with an email address and a password.\n"
        "- A wrong password keeps the user on the sign-in page with an error.\n"
    )
    body = _chat(workspace, message=prd)
    assert body["intent"] == "spec_check"
    assert body["test_suite"] is None
    result = body["prd"]
    assert result["verdict"] == "aligned"
    assert [item["id"] for item in result["requirements"]] == ["R1", "R2"]
    assert result["content"] == prd


def _report_body(fmt: str) -> dict:
    return {
        "format": fmt,
        "suite": {
            "suite_id": LOGIN_SUITE["suite_id"],
            "module_key": "/login",
            "module_name": "Login",
            "feature": "Login",
            "version": 3,
            "cases": [
                {
                    "case_id": "c1",
                    "kind": "success",
                    "title": "Valid sign-in reaches the studio",
                    "requirement_ids": ["R1"],
                    "steps": [
                        {
                            "id": "s1",
                            "label": "Open sign in",
                            "actions": [{"type": "navigate", "path": "/login"}],
                        },
                        {
                            "id": "s2",
                            "label": "Enter the account",
                            "actions": [
                                {"type": "fill", "selectors": ["#email"], "value": "a@b.test"},
                                {"type": "fill", "selectors": ["#password"], "value": "hunter2"},
                            ],
                        },
                    ],
                    "runs": [
                        {"run_id": "r1", "status": "passed", "finished_at": "2026-10-25T10:00:00Z"},
                    ],
                },
                {
                    "case_id": "c2",
                    "kind": "failure",
                    "title": "Wrong password shows an error",
                    "requirement_ids": ["R2"],
                    "runs": [
                        {"run_id": "r2a", "status": "passed", "finished_at": "2026-10-24T10:00:00Z"},
                        {
                            "run_id": "r2b",
                            "status": "failure",
                            "status_message": "Step “Submit” failed: timeout…",
                            "failed_step": "Submit login form",
                            "run_folder": "2026-10-25_10-05-00_login_v3_02-wrong-password",
                            "screenshots": ["https://example.test/s.jpg"],
                            "finished_at": "2026-10-25T10:05:00Z",
                        },
                    ],
                },
                {"case_id": "c3", "kind": "edge", "title": "Password stays masked", "runs": []},
            ],
        },
        "prd": {
            "title": "Sign in",
            "requirements": [
                {"id": "R1", "text": "Email sign-in", "status": "implemented"},
                {"id": "R2", "text": "Wrong password error", "status": "implemented"},
                {"id": "R3", "text": "Remember me", "status": "missing", "gap": "No checkbox"},
            ],
        },
    }


def test_markdown_report_counts_from_the_latest_runs() -> None:
    res = TestClient(app).post("/v1/report", json=_report_body("md"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["metrics"] == {
        "total": 3,
        "executed": 2,
        "passed": 1,
        "failed": 1,
        "not_run": 1,
        "runs": 3,
    }
    assert body["filename"].startswith("login_v3_report_")
    assert body["filename"].endswith(".md")
    text = body["content"]
    assert "| 3 | 2 | 1 | 1 | 1 | 50% |" in text
    assert f"| Suite ID | `{LOGIN_SUITE['suite_id']}` |" in text
    assert "### Wrong password shows an error" in text
    assert "- Failing step: Submit login form" in text
    # Every case lists its id and its latest run id; the newest run comes first.
    assert "- Case ID: `c2`" in text
    assert "- Latest run ID: `r2b`" in text
    assert "- Run ID: `r2b`" in text
    # Steps are spelled out, with secret values masked.
    assert '`Type "a@b.test" into #email`' in text
    assert '`Type "********" into #password`' in text
    assert "hunter2" not in text
    # Without the model, the flow falls back to the success case's steps.
    assert "```mermaid" in text
    assert 'n0["Open sign in"]' in text
    assert "| R3: Remember me | missing (No checkbox) | - | Not covered |" in text
    assert "| R2: Wrong password error | implemented | Wrong password shows an error | Failing |" in text
    # The spec check's outcome comes before the matrix.
    assert (
        "**PRD alignment:** The implementation has gaps against the PRD. Of 3 "
        "requirements, 2 are implemented, 0 partially and 1 are missing."
    ) in text
    assert "- **R3** (missing): Remember me Gap: No checkbox" in text


def test_pdf_report_is_a_pdf() -> None:
    res = TestClient(app).post("/v1/report", json=_report_body("pdf"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["encoding"] == "base64"
    assert body["content_type"] == "application/pdf"
    assert base64.b64decode(body["content"]).startswith(b"%PDF")


def test_report_uses_the_model_prose_and_a_safe_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURSOR_AI_MOCK_LLM", raising=False)

    async def fake_completion(**kwargs):
        assert kwargs["purpose"] == "report analysis"
        context = json.loads(kwargs["user_content"])
        assert [case["case_id"] for case in context["cases"]] == ["c1", "c2", "c3"]
        return json.dumps(
            {
                "feature_overview": "**Login** lets a user into the studio.",
                "how_it_works": ["Open sign in.", "Submit the form."],
                "flow": {
                    "nodes": [
                        {"id": "a", "label": "Sign in"},
                        {"id": "b", "label": "Studio"},
                        {"id": "c", "label": "Error"},
                    ],
                    "edges": [
                        {"from": "a", "to": "b", "label": "valid"},
                        {"from": "a", "to": "c", "label": "invalid"},
                        {"from": "c", "to": "a", "label": "retry"},
                        {"from": "a", "to": "ghost", "label": ""},
                    ],
                },
                "overview": "Half of the executed cases passed.",
                "highlights": ["Sign-in works."],
                "cases": [
                    {
                        "case_id": "c1",
                        "what_it_checks": "The happy path.",
                        "expected": "Studio opens.",
                    }
                ],
                "failures": [
                    {"case_id": "c2", "cause": "Submit timed out.", "next_step": "Check the API."}
                ],
                "recommendations": ["Fix the submit timeout."],
            }
        )

    monkeypatch.setattr("app.llm_service.json_completion", fake_completion)
    body = _report_body("md")
    body.update(generation_model="gpt-5.6", api_key="sk-test")
    res = TestClient(app).post("/v1/report", json=body)
    assert res.status_code == 200, res.text
    text = res.json()["content"]
    assert "**Login** lets a user into the studio." in text
    assert "**What it checks:** The happy path." in text
    assert "**Expected result:** Studio opens." in text
    assert "- Likely cause: Submit timed out." in text
    assert "- Fix the submit timeout." in text
    # The loop back and the unknown node are dropped so the chart can be drawn top-down.
    assert 'n0 -->|"valid"| n1' in text
    assert "retry" not in text
    assert "ghost" not in text


def test_a_numbered_prd_keeps_its_own_requirements(workspace) -> None:
    prd = (
        "# PRD: Create Account (Atelier Canvas)\n\n"
        "## Overview\nNew users create an account.\n\n"
        "## Requirements\n\n"
        "R1. The link opens the sign-up page at /register.\n\n"
        "R2. Sign-up is a single page. All fields are shown at once\n"
        "    with one \"Create account\" button. There is no wizard.\n\n"
        "R3. The password must be at least 8 characters and include one uppercase\n"
        "    letter, one number and one symbol. A strength meter is shown.\n\n"
        "**AC-4:** A duplicate email shows an error.\n"
    )
    body = _chat(workspace, message=prd)
    assert body["intent"] == "spec_check"
    result = body["prd"]
    assert result["title"] == "Create Account (Atelier Canvas)"
    assert [item["id"] for item in result["requirements"]] == ["R1", "R2", "R3", "AC-4"]
    third = result["requirements"][2]["text"]
    assert third.startswith("The password must be at least 8 characters")
    assert third.endswith("A strength meter is shown.")
