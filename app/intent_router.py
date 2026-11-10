"""Decide the chat path for one turn. The model decides; nothing here matches keywords."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Intents the router returns.
OUT_OF_SCOPE = "out_of_scope"
GENERAL_QA = "general_qa"
FEATURE = "feature"
CONFIRM = "confirm"
DECLINE = "decline"
CHANGE_SUITE = "change_suite"
PROVIDE_DATA = "provide_data"
SPEC_CHECK = "spec_check"
SUITE_STATUS = "suite_status"

# Intents a finished turn reports back (stored as the conversation's last intent).
TEST_GENERATION = "test_generation"
SUITE_REUSED = "suite_reused"
DECLINED = "declined"

# What Tursor asked at the end of its last reply; the next "yes" acts on it.
PENDING_GENERATE = "generate"
PENDING_REUSE = "reuse"
PENDING_NEW_VERSION = "new_version"
# Tursor asked for values; once they are all in, it generates without asking again.
PENDING_COLLECT_GENERATE = "collect_generate"
PENDING_COLLECT_NEW_VERSION = "collect_new_version"
PENDING_COLLECT = {PENDING_COLLECT_GENERATE, PENDING_COLLECT_NEW_VERSION}
PENDING_ACTIONS = {
    PENDING_GENERATE,
    PENDING_REUSE,
    PENDING_NEW_VERSION,
    *PENDING_COLLECT,
}

CLASSIFIED_INTENTS = {
    OUT_OF_SCOPE,
    GENERAL_QA,
    FEATURE,
    CONFIRM,
    DECLINE,
    CHANGE_SUITE,
    PROVIDE_DATA,
    SPEC_CHECK,
    SUITE_STATUS,
}

OUT_OF_SCOPE_REPLY = (
    "That's outside what I can help with. I can explain flows in this app, "
    "answer testing and QA questions, and build and run CDP test suites."
)

_MAX_SCREENS = 200
_MAX_CASE = 3000

_QUESTION = {
    PENDING_GENERATE: "Generate the test suite for this feature?",
    PENDING_REUSE: "Reuse the saved test suite as it is?",
    PENDING_NEW_VERSION: "Generate the next version of the saved test suite?",
    PENDING_COLLECT_GENERATE: "Which values should the tests use? (asked for test data)",
    PENDING_COLLECT_NEW_VERSION: "Which values should the tests use? (asked for test data)",
}


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    screens: list[str]
    suite_id: str
    reason: str


def _request_payload(
    *,
    message: str,
    case: str,
    pending_action: str,
    pending_suite: dict[str, Any] | None,
    active_suite_id: str,
    nodes: list[dict[str, Any]],
    suites: list[dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "message": message,
            "case": case[:_MAX_CASE],
            "pending_question": (
                {
                    "question": _QUESTION[pending_action],
                    "suite": (
                        pending_suite.get("module_name") or pending_suite.get("feature")
                        if pending_suite
                        else None
                    ),
                }
                if pending_action in _QUESTION
                else None
            ),
            "active_suite": active_suite_id or None,
            "screens": [
                {
                    "module_key": node.get("route"),
                    "name": node.get("name"),
                    "summary": node.get("summary", ""),
                }
                for node in nodes[:_MAX_SCREENS]
            ],
            "existing_suites": [
                {
                    "suite_id": suite.get("suite_id"),
                    "name": suite.get("module_name") or suite.get("feature"),
                    "module_key": suite.get("module_key"),
                }
                for suite in suites
            ],
        },
        indent=2,
    )


def parse_decision(
    data: dict[str, Any],
    *,
    known_screens: set[str],
    known_suites: set[str],
    pending_action: str,
    active_suite_id: str,
) -> IntentDecision:
    """Keep only values that exist and answers that make sense right now."""
    intent = data.get("intent")
    if intent not in CLASSIFIED_INTENTS:
        intent = FEATURE
    # A yes or no needs a question to answer; a change needs a suite to change.
    if intent in {CONFIRM, DECLINE} and pending_action not in PENDING_ACTIONS:
        intent = FEATURE
    # Values only answer a request for values; a "yes" to that request also counts.
    if intent == PROVIDE_DATA and pending_action not in PENDING_COLLECT:
        intent = FEATURE
    if intent == CONFIRM and pending_action in PENDING_COLLECT:
        intent = PROVIDE_DATA
    if intent == CHANGE_SUITE and not active_suite_id:
        intent = FEATURE
    # Questions about runs need a saved suite; without one it is a general question.
    if intent == SUITE_STATUS and not known_suites:
        intent = GENERAL_QA
    screens = [
        key
        for key in data.get("screens") or []
        if isinstance(key, str) and key in known_screens
    ]
    suite_id = data.get("suite_id")
    if not isinstance(suite_id, str) or suite_id not in known_suites:
        suite_id = ""
    reason = data.get("reason")
    return IntentDecision(
        intent=str(intent),
        screens=screens,
        suite_id=suite_id,
        reason=reason.strip()[:300] if isinstance(reason, str) else "",
    )


async def classify_intent(
    *,
    message: str,
    case: str,
    pending_action: str,
    pending_suite_id: str,
    active_suite_id: str,
    nodes: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    generation_model: str,
    api_key: str,
) -> IntentDecision:
    # Imported here so llm_service and mock_intent can import the constants above.
    from app.llm_service import json_completion, loads_object, mock_enabled
    from app.prompts import INTENT_PROMPT

    if mock_enabled():
        from app.mock_intent import mock_classify

        return mock_classify(
            message=message,
            case=case,
            pending_action=pending_action,
            active_suite_id=active_suite_id,
            nodes=nodes,
            suites=suites,
        )

    pending_suite = next(
        (s for s in suites if s.get("suite_id") == pending_suite_id), None
    )
    content = await json_completion(
        generation_model=generation_model,
        api_key=api_key,
        system_prompt=INTENT_PROMPT,
        user_content=_request_payload(
            message=message,
            case=case,
            pending_action=pending_action,
            pending_suite=pending_suite,
            active_suite_id=active_suite_id,
            nodes=nodes,
            suites=suites,
        ),
        purpose="intent",
    )
    return parse_decision(
        loads_object(content),
        known_screens={str(node.get("route")) for node in nodes},
        known_suites={str(suite.get("suite_id")) for suite in suites},
        pending_action=pending_action,
        active_suite_id=active_suite_id,
    )
