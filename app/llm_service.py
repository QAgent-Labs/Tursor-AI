from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.prompts import INTRO_USER_PROMPT, SYSTEM_PROMPT


class LlmError(Exception):
    pass


def _wants_cdp_steps(message: str) -> bool:
    text = message.lower().strip().rstrip(".!")
    if text in {"yes", "y", "ok", "okay", "sure", "go ahead", "do it", "please do"}:
        return True
    phrases = (
        "cdp step",
        "cdp steps",
        "create the step",
        "create me the",
        "generate the step",
        "generate steps",
        "generate the cdp",
        "start the test",
        "let's start",
        "lets start",
    )
    return any(phrase in text for phrase in phrases)


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


def _mock_response(mode: str, message: str, case: str) -> dict[str, Any]:
    if mode == "intro":
        return {
            "reply": (
                "I can explain this codebase and create CDP steps when you want to test a flow. "
                "Ask how something works, or tell me to create the steps."
            ),
            "case": "No requirement yet.",
            "cdp_steps": None,
        }
    if _wants_cdp_steps(message):
        return {
            "reply": "Created CDP steps for the login flow. Use Run Test when you want to execute them.",
            "case": case or "User asked for CDP steps for the login flow.",
            "cdp_steps": _mock_login_steps(),
        }
    return {
        "reply": (
            f"(Mock LLM) {message[:300]}. "
            "Say yes or ask me to create the CDP steps when you want a runnable plan."
        ),
        "case": case or message[:300],
        "cdp_steps": None,
    }


def _build_user_payload(
    *,
    mode: str,
    message: str,
    case: str,
    plans: list[dict[str, str]],
    latest_cdp_steps: list[dict[str, Any]] | None,
    retrieved_context: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if case.strip():
        parts.append(f"case:\n{case.strip()}")
    if plans:
        parts.append(f"plans:\n{json.dumps(plans, indent=2)}")
    if latest_cdp_steps:
        parts.append(f"latest_cdp_steps:\n{json.dumps(latest_cdp_steps, indent=2)}")
    if retrieved_context:
        parts.append("retrieved_workspace_context:")
        for chunk in retrieved_context[:12]:
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
    return {
        "reply": reply.strip(),
        "case": case.strip(),
        "cdp_steps": _normalize_steps(data.get("cdp_steps")),
    }


async def complete_chat(
    *,
    generation_model: str,
    api_key: str,
    mode: str,
    message: str,
    case: str = "",
    plans: list[dict[str, str]] | None = None,
    latest_cdp_steps: list[dict[str, Any]] | None = None,
    retrieved_context: list[dict[str, Any]] | None = None,
    openai_base_url: str = "https://api.openai.com/v1",
) -> dict[str, Any]:
    if os.environ.get("TURSOR_AI_MOCK_LLM", "").strip() in ("1", "true", "yes"):
        return _mock_response(mode, message, case)

    user_content = _build_user_payload(
        mode=mode,
        message=message,
        case=case,
        plans=plans or [],
        latest_cdp_steps=latest_cdp_steps,
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

    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            f"{openai_base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )

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
