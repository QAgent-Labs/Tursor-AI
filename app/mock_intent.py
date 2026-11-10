"""Intent rules for mock mode (TURSOR_AI_MOCK_LLM) and tests only.

Production routing is decided by the model in intent_router.classify_intent. These
keyword rules exist so the test suite and offline demos run without an LLM; they are
never used when a real model is configured.
"""

from __future__ import annotations

import re
from typing import Any

from app.intent_router import (
    CHANGE_SUITE,
    CONFIRM,
    DECLINE,
    FEATURE,
    GENERAL_QA,
    OUT_OF_SCOPE,
    PENDING_ACTIONS,
    PENDING_COLLECT,
    PROVIDE_DATA,
    SPEC_CHECK,
    SUITE_STATUS,
    IntentDecision,
)

_CONFIRMATIONS = {
    "yes",
    "y",
    "ok",
    "okay",
    "sure",
    "go ahead",
    "go on",
    "do it",
    "please do",
    "proceed",
    "yes go on",
    "yes go ahead",
    "generate",
    "generate it",
}

_TEST_PHRASES = (
    "test suite",
    "test this",
    "test the ",
    "cdp step",
    "cdp steps",
    "create the step",
    "create me the",
    "generate the step",
    "generate steps",
    "generate the suite",
    "generate a suite",
    "generate the test",
    "generate a test",
    "generate the cdp",
    "write the test",
    "run this",
)

_OUT_OF_SCOPE_PHRASES = (
    "weather",
    "stock price",
    "tell me a joke",
    "write me a joke",
    "python game",
    "write me a game",
    "recipe for",
)


def normalize_message(message: str) -> str:
    text = message.lower().strip().replace("'", "'")
    for mark in "?!.,":
        text = text.replace(mark, "")
    return " ".join(text.split())


def is_confirmation(message: str) -> bool:
    return normalize_message(message) in _CONFIRMATIONS


_DECLINES = {
    "no",
    "n",
    "nope",
    "no thanks",
    "dont",
    "do not",
    "create new",
    "create a new one",
    "new one",
    "regenerate",
    "start fresh",
    "generate new",
    "generate a new one",
}

_DECLINE_PHRASES = (
    "don't reuse",
    "dont reuse",
    "do not reuse",
    "not reuse",
    "new suite",
    "fresh suite",
)


def is_decline(message: str) -> bool:
    text = normalize_message(message)
    return text in _DECLINES or any(phrase in text for phrase in _DECLINE_PHRASES)


_PRD_MARKERS = (
    "acceptance criteria",
    "as a user",
    "user story",
    "requirement",
    " shall ",
    " must ",
    " should ",
    "functional",
    "scope",
)


def looks_like_prd(message: str) -> bool:
    """A pasted PRD: named as one, or long text written as requirements."""
    text = f" {normalize_message(message)} "
    if re.search(r"\bprd\b", text) or "product requirement" in text:
        return True
    if len(message) < 500:
        return False
    return sum(text.count(marker) for marker in _PRD_MARKERS) >= 3


def _is_test_request(text: str) -> bool:
    return any(phrase in text for phrase in _TEST_PHRASES)


_GENERIC_WORDS = {
    "feature",
    "screen",
    "page",
    "flow",
    "button",
    "testing",
    "suite",
    "account",
}

_STOP_WORDS = {
    "about",
    "between",
    "could",
    "define",
    "definition",
    "difference",
    "different",
    "does",
    "example",
    "examples",
    "explain",
    "hello",
    "please",
    "should",
    "something",
    "thank",
    "thanks",
    "their",
    "there",
    "these",
    "those",
    "understand",
    "want",
    "wants",
    "what's",
    "whats",
    "where",
    "which",
    "would",
    "work",
    "works",
    "working",
}

_CONCEPT_WORDS = {
    "acceptance",
    "accessibility",
    "assertion",
    "assertions",
    "automation",
    "bdd",
    "boundary",
    "concept",
    "concepts",
    "coverage",
    "e2e",
    "equivalence",
    "exploratory",
    "fixture",
    "fixtures",
    "flake",
    "flaky",
    "integration",
    "manual",
    "mock",
    "mocking",
    "mocks",
    "performance",
    "qa",
    "regression",
    "sanity",
    "smoke",
    "stress",
    "stub",
    "stubs",
    "tdd",
    "traceability",
    "uat",
    "unit",
    "usability",
}

# Phrases that make a message a question about testing itself, not about this app.
_CONCEPT_PHRASES = (
    "test case",
    "test plan",
    "test strategy",
    "test pyramid",
    "best practice",
    "black box",
    "white box",
    "end to end",
    "end-to-end",
    "load test",
    "difference between",
    "types of test",
    "kinds of test",
    "how to write",
    "how do i write",
)


def is_testing_question(message: str) -> bool:
    """A question about testing or QA in general: concepts, kinds of tests, practice."""
    text = normalize_message(message)
    words = set(text.split())
    return bool(words & _CONCEPT_WORDS) or any(
        phrase in text for phrase in _CONCEPT_PHRASES
    )


def is_out_of_scope(message: str) -> bool:
    text = normalize_message(message)
    return any(
        phrase in text for phrase in _OUT_OF_SCOPE_PHRASES
    ) and not _is_test_request(text)


def wants_generation(message: str) -> bool:
    return _is_test_request(normalize_message(message))


_EMPTY_CASE = "no requirement has been given yet"


def names_a_flow(message: str, case: str) -> bool:
    recorded = normalize_message(case)
    if recorded == _EMPTY_CASE:
        recorded = ""
    text = normalize_message(f"{message}\n{recorded}")
    for word in text.split():
        if len(word) <= 4:
            continue
        if word in _GENERIC_WORDS or word in _STOP_WORDS or word in _CONCEPT_WORDS:
            continue
        return True
    return False


def _name_in_text(name: str, text: str) -> bool:
    normalized = normalize_message(name)
    if len(normalized) >= 4 and f" {normalized} " in f" {text} ":
        return True
    words = [
        word
        for word in normalized.split()
        if len(word) > 4 and word not in _GENERIC_WORDS
    ]
    return any(f" {word} " in f" {text} " for word in words)


def mentions(name: str, message: str, case: str = "") -> bool:
    return bool(name) and _name_in_text(name, normalize_message(f"{message}\n{case}"))


def _route_words(route: str) -> str:
    segments = [
        segment
        for segment in route.strip("/").split("/")
        if segment and not segment.startswith(":")
    ]
    return " ".join(segment.replace("-", " ") for segment in segments)


def nodes_named(
    message: str,
    case: str,
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flow-map screens the message or the case names, best match first."""
    text = normalize_message(f"{message}\n{case}")
    scored: list[tuple[int, dict[str, Any]]] = []
    for node in nodes:
        name = node.get("name")
        route = node.get("route") or node.get("module_key") or ""
        score = 0
        if isinstance(name, str) and _name_in_text(name, text):
            score = 2 + len(normalize_message(name))
        elif (
            isinstance(route, str)
            and route != "/"
            and _name_in_text(_route_words(route), text)
        ):
            score = 1
        if score:
            scored.append(
                (
                    score,
                    {key: value for key, value in node.items() if key != "embedding"},
                )
            )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [node for _, node in scored]


_RUN_QUESTION = re.compile(
    r"\b(did|which|how many|what)\b.*\b(fail|failed|pass|passed|run|runs|ran)\b",
    re.IGNORECASE,
)


def asks_about_runs(message: str) -> bool:
    return bool(_RUN_QUESTION.search(message)) and message.strip().endswith("?")


def mock_classify(
    *,
    message: str,
    case: str,
    pending_action: str,
    active_suite_id: str,
    nodes: list[dict[str, Any]],
    suites: list[dict[str, Any]],
) -> IntentDecision:
    if is_out_of_scope(message):
        return IntentDecision(OUT_OF_SCOPE, [], "", "mock: out-of-scope phrase")
    if looks_like_prd(message):
        return IntentDecision(SPEC_CHECK, [], "", "mock: looks like a PRD")
    if suites and asks_about_runs(message):
        return IntentDecision(SUITE_STATUS, [], "", "mock: question about runs")
    pending = pending_action in PENDING_ACTIONS
    if pending_action in PENDING_COLLECT and not is_decline(message):
        return IntentDecision(PROVIDE_DATA, [], "", "mock: values supplied")
    if pending and is_confirmation(message):
        return IntentDecision(CONFIRM, [], "", "mock: confirmed")
    if pending and is_decline(message):
        return IntentDecision(DECLINE, [], "", "mock: declined")
    named = nodes_named(message, case, nodes)
    screens = [str(node.get("route") or node.get("module_key")) for node in named]
    if active_suite_id and wants_generation(message) and not named:
        return IntentDecision(CHANGE_SUITE, [], "", "mock: change the working suite")
    general = is_testing_question(message) or not names_a_flow(message, case)
    if not named and not wants_generation(message) and general:
        return IntentDecision(GENERAL_QA, [], "", "mock: no screen named")
    suite_id = ""
    for key in screens:
        suite_id = next(
            (str(s["suite_id"]) for s in suites if s.get("module_key") == key), ""
        )
        if suite_id:
            break
    if not suite_id:
        for suite in suites:
            for name in (suite.get("module_name"), suite.get("feature")):
                if isinstance(name, str) and mentions(name, message, case):
                    suite_id = str(suite["suite_id"])
                    break
            if suite_id:
                break
    return IntentDecision(FEATURE, screens, suite_id, "mock: rules")
