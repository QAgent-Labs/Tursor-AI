from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.prompts import INTRO_REPLY, INTRO_USER_PROMPT, SYSTEM_PROMPT


class LlmError(Exception):
    pass


def _wants_test_suite(message: str) -> bool:
    text = message.lower().strip().rstrip(".!")
    if text in {"yes", "y", "ok", "okay", "sure", "go ahead", "do it", "please do", "proceed"}:
        return True
    phrases = (
        "test suite",
        "cdp step",
        "cdp steps",
        "create the step",
        "create me the",
        "generate the step",
        "generate steps",
        "generate the suite",
        "generate the cdp",
        "start the test",
        "let's start",
        "lets start",
    )
    return any(phrase in text for phrase in phrases)


def _mock_suite() -> dict[str, Any]:
    return {
        "feature": "Login",
        "cases": [
            {
                "kind": "success",
                "title": "Success test case",
                "explanation": "Signs in with valid credentials and reaches /done.",
                "steps": _mock_login_steps(),
            },
            {
                "kind": "failure",
                "title": "Failure test case",
                "explanation": "Submits the form without credentials and waits for the app's error text.",
                "steps": [
                    {
                        "id": "navigate-home",
                        "label": "Navigate to home page",
                        "actions": [{"type": "navigate", "path": "/"}],
                    },
                    {
                        "id": "submit-empty",
                        "label": "Submit empty login",
                        "actions": [
                            {
                                "type": "click",
                                "selectors": [
                                    '[data-testid="submit"]',
                                    'button[type="submit"]',
                                ],
                            }
                        ],
                    },
                    {
                        "id": "assert-error",
                        "label": "Confirm the error",
                        "actions": [
                            {
                                "type": "waitForText",
                                "text": "Invalid",
                                "timeoutMs": 15000,
                            }
                        ],
                    },
                ],
            },
            {
                "kind": "edge",
                "title": "Password field stays masked",
                "explanation": "The password control is an input of type password.",
                "steps": [
                    {
                        "id": "navigate-home",
                        "label": "Navigate to home page",
                        "actions": [{"type": "navigate", "path": "/"}],
                    },
                    {
                        "id": "fill-password",
                        "label": "Fill password",
                        "actions": [
                            {
                                "type": "fill",
                                "selectors": [
                                    '[data-testid="password"]',
                                    'input[type="password"]',
                                ],
                                "value": "demo-pass",
                            }
                        ],
                    },
                ],
            },
        ],
    }


def _mock_login_steps() -> list[dict[str, Any]]:
    return [
        {
            "id": "navigate-home",
            "label": "Navigate to home page",
            "actions": [{"type": "navigate", "path": "/"}],
        },
        {
            "id": "click-get-started",
            "label": "Click Get started",
            "actions": [
                {
                    "type": "click",
                    "selectors": [
                        '[data-testid="get-started"]',
                        'button:has-text("Get started")',
                    ],
                }
            ],
        },
        {
            "id": "fill-credentials",
            "label": "Fill username and password",
            "actions": [
                {
                    "type": "fill",
                    "selectors": ['[data-testid="username"]', 'input[name="username"]'],
                    "value": "demo-user",
                },
                {
                    "type": "fill",
                    "selectors": ['[data-testid="password"]', 'input[type="password"]'],
                    "value": "demo-pass",
                },
            ],
        },
        {
            "id": "submit-form",
            "label": "Submit login form",
            "actions": [
                {
                    "type": "click",
                    "selectors": ['[data-testid="submit"]', 'button[type="submit"]'],
                }
            ],
        },
        {
            "id": "assert-done",
            "label": "Confirm /done",
            "actions": [{"type": "waitForPath", "pathIncludes": "/done", "timeoutMs": 15000}],
        },
    ]


def _mock_response(
    mode: str,
    message: str,
    case: str,
    brief_summary: str,
) -> dict[str, Any]:
    if mode == "intro":
        return {
            "reply": INTRO_REPLY,
            "case": "No requirement has been given yet.",
            "brief_summary": "No requirement has been given yet.",
            "test_suite": None,
        }
    if _wants_test_suite(message):
        return {
            "reply": (
                "- ##Success test case##\n"
                "Signs in with valid credentials and reaches /done.\n"
                "- ##Failure test case##\n"
                "Submits the form without credentials and waits for the app's error text.\n"
                "- ##Edge cases##\n"
                "-- ##Password field stays masked##\n"
                "The password control is an input of type password."
            ),
            "case": case or "- User confirmed a CDP test suite for ##Login##.",
            "brief_summary": brief_summary or "Login test suite was created.",
            "test_suite": _mock_suite(),
        }
    gist = brief_summary or (case or message[:160]).replace("\n", " ")
    return {
        "reply": (
            f"(Mock LLM) {message[:300]}.\n"
            "\n"
            "Shall I proceed with generating the test suite for Login?"
        ),
        "case": case or message[:300],
        "brief_summary": gist[:180],
        "test_suite": None,
    }


def _build_user_payload(
    *,
    mode: str,
    message: str,
    case: str,
    brief_summary: str,
    plans: list[dict[str, Any]],
    cdp_runs: list[dict[str, Any]],
    latest_cdp_steps: list[dict[str, Any]] | None,
    latest_test_suite: dict[str, Any] | None,
    retrieved_context: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if case.strip():
        parts.append(f"case:\n{case.strip()}")
    if brief_summary.strip():
        parts.append(f"brief_summary:\n{brief_summary.strip()}")
    if plans:
        parts.append(f"plans:\n{json.dumps(plans, indent=2)}")
    if cdp_runs:
        parts.append(f"cdp_runs:\n{json.dumps(cdp_runs, indent=2)}")
    if latest_test_suite:
        parts.append(f"latest_test_suite:\n{json.dumps(latest_test_suite, indent=2)}")
    elif latest_cdp_steps:
        parts.append(f"latest_cdp_steps:\n{json.dumps(latest_cdp_steps, indent=2)}")
    if retrieved_context:
        parts.append("retrieved_workspace_context:")
        for chunk in retrieved_context[:20]:
            path = chunk.get("path", "?")
            start = chunk.get("start_line", "?")
            end = chunk.get("end_line", "?")
            text = chunk.get("content", "")
            parts.append(f"--- {path}:{start}-{end} ---\n{text}\n")
    if mode == "intro":
        parts.append(INTRO_USER_PROMPT)
    else:
        parts.append(f"user_message:\n{message}")
    return "\n\n".join(parts)


def _normalize_steps(raw: object) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise LlmError("cdp_steps must be a list or null")
    if len(raw) == 0:
        return None
    steps: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise LlmError("each cdp step must be an object")
        step_id = item.get("id")
        label = item.get("label")
        actions = item.get("actions")
        if not isinstance(step_id, str) or not step_id.strip():
            raise LlmError("each cdp step needs an id")
        if not isinstance(label, str) or not label.strip():
            raise LlmError("each cdp step needs a label")
        if not isinstance(actions, list) or not actions:
            raise LlmError("each cdp step needs a non-empty actions list")
        steps.append({"id": step_id.strip(), "label": label.strip(), "actions": actions})
    return steps


def _normalize_suite(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LlmError("test_suite must be an object or null")
    feature = raw.get("feature")
    cases = raw.get("cases")
    if not isinstance(feature, str) or not feature.strip():
        raise LlmError("test_suite needs a feature")
    if not isinstance(cases, list):
        raise LlmError("test_suite.cases must be a list")
    grouped: dict[str, list[dict[str, Any]]] = {
        "success": [],
        "failure": [],
        "edge": [],
    }
    for item in cases:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in grouped:
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        explanation = item.get("explanation")
        if not isinstance(explanation, str):
            explanation = ""
        steps = _normalize_steps(item.get("steps"))
        if not steps:
            continue
        grouped[kind].append(
            {
                "kind": kind,
                "title": title.strip(),
                "explanation": explanation.strip(),
                "steps": steps,
            }
        )
    ordered = [*grouped["success"], *grouped["failure"], *grouped["edge"]]
    ordered = ordered[:40]
    if not ordered:
        return None
    return {"feature": feature.strip(), "cases": ordered}


def _parse_json_response(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n") if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmError(f"LLM returned non-JSON: {text[:300]}") from exc
    if not isinstance(data, dict):
        raise LlmError("LLM JSON must be an object")
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise LlmError("LLM JSON must include a reply string")
    case = data.get("case") if isinstance(data.get("case"), str) else ""
    brief = data.get("brief_summary") if isinstance(data.get("brief_summary"), str) else ""
    suite = _normalize_suite(data.get("test_suite"))
    if suite is None:
        legacy = _normalize_steps(data.get("cdp_steps"))
        if legacy:
            suite = {
                "feature": "Feature",
                "cases": [
                    {
                        "kind": "success",
                        "title": "Success test case",
                        "explanation": "",
                        "steps": legacy,
                    }
                ],
            }
    return {
        "reply": reply.strip(),
        "case": case.strip(),
        "brief_summary": brief.strip(),
        "test_suite": suite,
    }


async def complete_chat(
    *,
    generation_model: str,
    api_key: str,
    mode: str,
    message: str,
    case: str = "",
    brief_summary: str = "",
    plans: list[dict[str, Any]] | None = None,
    cdp_runs: list[dict[str, Any]] | None = None,
    latest_cdp_steps: list[dict[str, Any]] | None = None,
    latest_test_suite: dict[str, Any] | None = None,
    retrieved_context: list[dict[str, Any]] | None = None,
    openai_base_url: str = "https://api.openai.com/v1",
) -> dict[str, Any]:
    if mode == "intro" or os.environ.get("TURSOR_AI_MOCK_LLM", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        return _mock_response(mode, message, case, brief_summary)

    user_content = _build_user_payload(
        mode=mode,
        message=message,
        case=case,
        brief_summary=brief_summary,
        plans=plans or [],
        cdp_runs=cdp_runs or [],
        latest_cdp_steps=latest_cdp_steps,
        latest_test_suite=latest_test_suite,
        retrieved_context=retrieved_context or [],
    )

    payload = {
        "model": generation_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 1,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            res = await client.post(
                f"{openai_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise LlmError(
            "The model took too long to answer. Send the message again."
        ) from exc

    if res.status_code >= 400:
        raise LlmError(f"OpenAI HTTP {res.status_code}: {res.text[:500]}")

    body = res.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"Unexpected OpenAI response shape: {body}") from exc

    if not isinstance(content, str):
        raise LlmError("OpenAI message content is not a string")

    return _parse_json_response(content)
