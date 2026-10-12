"""Decide the chat path before any retrieval. Rules only, so this step does not call a model."""

from __future__ import annotations

OUT_OF_SCOPE = "out_of_scope"
GENERAL_QA = "general_qa"
FEATURE = "feature"
TEST_GENERATION = "test_generation"

OUT_OF_SCOPE_REPLY = (
    "That's outside Tursor's scope. I can help with application testing, "
    "QA concepts, test flows, and generating CDP suites."
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
    "could",
    "does",
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
    "which",
    "would",
    "work",
    "works",
    "working",
}

_CONCEPT_WORDS = {
    "assertion",
    "automation",
    "concept",
    "concepts",
    "coverage",
    "flake",
    "flaky",
    "integration",
    "manual",
    "mock",
    "mocks",
    "regression",
    "smoke",
    "stub",
    "stubs",
}


def is_out_of_scope(message: str) -> bool:
    text = normalize_message(message)
    return any(phrase in text for phrase in _OUT_OF_SCOPE_PHRASES) and not _is_test_request(text)


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


def features_named(
    message: str,
    case: str,
    features: list[dict],
) -> list[dict]:
    text = normalize_message(f"{message}\n{case}")
    matched: list[dict] = []
    for item in features:
        name = item.get("feature")
        if not isinstance(name, str) or not _name_in_text(name, text):
            continue
        matched.append({key: value for key, value in item.items() if key != "embedding"})
    return matched
