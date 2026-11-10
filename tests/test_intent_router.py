import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.embed_service import build_embeddings
from app.intent_router import classify_intent, parse_decision
from app.main import app
from app.mock_intent import (
    is_decline,
    is_out_of_scope,
    is_testing_question,
    looks_like_prd,
    names_a_flow,
    nodes_named,
    wants_generation,
)
from app.settings import settings


def test_the_model_decides_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURSOR_AI_MOCK_LLM", raising=False)
    seen: dict[str, str] = {}

    async def fake_completion(**kwargs):
        seen["purpose"] = kwargs["purpose"]
        seen["payload"] = kwargs["user_content"]
        return json.dumps(
            {
                "intent": "feature",
                "screens": ["/login", "/not-a-screen"],
                "suite_id": "s1",
                "reason": "asks for login tests",
            }
        )

    monkeypatch.setattr("app.llm_service.json_completion", fake_completion)
    decision = asyncio.run(
        classify_intent(
            message="cover sign in please",
            case="",
            pending_action="",
            pending_suite_id="",
            active_suite_id="",
            nodes=[{"route": "/login", "name": "Sign in"}],
            suites=[{"suite_id": "s1", "module_name": "Sign in", "module_key": "/login"}],
            generation_model="gpt-5.6",
            api_key="sk-test",
        )
    )
    assert seen["purpose"] == "intent"
    assert '"module_key": "/login"' in seen["payload"]
    assert decision.intent == "feature"
    assert decision.screens == ["/login"]
    assert decision.suite_id == "s1"
    assert decision.reason == "asks for login tests"


def test_unknown_values_from_the_model_are_dropped() -> None:
    decision = parse_decision(
        {"intent": "dance", "screens": ["/x"], "suite_id": "nope"},
        known_screens={"/login"},
        known_suites={"s1"},
        pending_action="",
        active_suite_id="",
    )
    assert decision.intent == "feature"
    assert decision.screens == []
    assert decision.suite_id == ""
    # A yes needs a question to answer, and a change needs a suite to change.
    for intent in ("confirm", "decline", "change_suite"):
        loose = parse_decision(
            {"intent": intent},
            known_screens=set(),
            known_suites=set(),
            pending_action="",
            active_suite_id="",
        )
        assert loose.intent == "feature", intent
    answered = parse_decision(
        {"intent": "confirm"},
        known_screens=set(),
        known_suites=set(),
        pending_action="reuse",
        active_suite_id="",
    )
    assert answered.intent == "confirm"


def test_chat_follows_the_model_for_a_general_question(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.hash_embeddings = True
    monkeypatch.delenv("TURSOR_AI_MOCK_LLM", raising=False)
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    build_embeddings(str(tmp_path))
    purposes: list[str] = []

    async def fake_completion(**kwargs):
        purposes.append(kwargs["purpose"])
        if kwargs["purpose"] == "intent":
            return '{"intent": "general_qa", "screens": [], "suite_id": "", "reason": "concept"}'
        return (
            '{"reply": "A smoke test is a quick check.", "case": "", '
            '"brief_summary": "", "test_suite": null}'
        )

    def fail_search(*_args, **_kwargs):
        raise AssertionError("a general question must not search the workspace")

    monkeypatch.setattr("app.llm_service.json_completion", fake_completion)
    monkeypatch.setattr("app.chat_service.search_workspace", fail_search)
    res = TestClient(app).post(
        "/v1/chat/completion",
        json={
            "conversation_id": "11111111-1111-4111-8111-111111111111",
            "workspace_path": str(tmp_path),
            "message": "whats smoke test",
            "generation_model": "gpt-5.6",
            "api_key": "sk-test",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "general_qa"
    assert body["reply"] == "A smoke test is a quick check."
    assert purposes == ["intent", "reply"]


# The rules below only back mock mode (TURSOR_AI_MOCK_LLM) and the offline tests.


def test_mock_rules_name_screens() -> None:
    nodes = [
        {"route": "/checkout/:workId", "name": "Print checkout"},
        {"route": "/login", "name": "Sign in"},
    ]
    assert nodes_named("hi i need to know how to test a feature", "", nodes) == []
    named = nodes_named("How does print checkout work?", "", nodes)
    assert [item["route"] for item in named] == ["/checkout/:workId"]
    upload = [{"route": "/studio/upload", "name": "Add work"}]
    assert [item["route"] for item in nodes_named("test the upload screen", "", upload)] == [
        "/studio/upload"
    ]


def test_mock_rules_detect_turn_kinds() -> None:
    assert is_out_of_scope("What's the weather?")
    assert wants_generation("okay create me the cdp steps for this flow please")
    assert not wants_generation("hi i need to know how to test a feature")
    assert names_a_flow("i want to test the account creation flow", "")
    assert not names_a_flow("hi", "No requirement has been given yet.")
    assert is_decline("create a new one")
    assert looks_like_prd("PRD: Sign in\nUsers sign in with email.")
    assert is_testing_question("whats smoke test")
    assert not is_testing_question("how does the checkout work?")
