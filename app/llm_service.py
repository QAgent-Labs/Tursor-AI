from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from app.log_relay import Timer, emit
from app.prompts import (
    INTRO_REPLY,
    INTRO_USER_PROMPT,
    PRD_EXTRACT_PROMPT,
    REPORT_PROMPT,
    SPEC_CHECK_PROMPT,
    SYSTEM_PROMPT,
)

OPENAI_BASE_URL = "https://api.openai.com/v1"


class LlmError(Exception):
    pass


def mock_enabled() -> bool:
    return os.environ.get("TURSOR_AI_MOCK_LLM", "").strip() in ("1", "true", "yes")


def _mock_suite() -> dict[str, Any]:
    return {
        "feature": "Login",
        "module_key": "/login",
        "module_name": "Login",
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
            "actions": [
                {"type": "waitForPath", "pathIncludes": "/done", "timeoutMs": 15000}
            ],
        },
    ]


def _mock_response(
    mode: str,
    message: str,
    case: str,
    brief_summary: str,
    intent: str = "",
) -> dict[str, Any]:
    if mode == "intro":
        return {
            "reply": INTRO_REPLY,
            "case": "No requirement has been given yet.",
            "brief_summary": "No requirement has been given yet.",
            "test_suite": None,
        }
    if intent == "general_qa":
        asks_for_a_feature = "feature" in message.lower() and not case.strip()
        return {
            "reply": "Which feature should I test?"
            if asks_for_a_feature
            else message[:300],
            "case": case,
            "brief_summary": brief_summary,
            "test_suite": None,
        }
    if intent == "feature":
        return {
            "reply": (
                "### How it works\n"
                "\n"
                "### Sign in\n"
                "- Fields: **Email** and **Password**.\n"
                "- **Sign in** submits the form.\n"
                "- Invalid credentials keep you on `/login` with an error.\n"
                "- A valid sign-in opens `/studio`.\n"
                "\n"
                "**Shall I generate the test suite for Sign in?**"
            ),
            "case": case or "Login flow.",
            "brief_summary": brief_summary or "Explained the login flow.",
            "ready": True,
            "test_suite": None,
        }
    if intent == "test_generation":
        return {
            "reply": (
                "Here is the **Login** suite: one success path to `/done` and one failure "
                "path that submits an empty form and waits for the app's error."
            ),
            "case": case or "User confirmed a CDP test suite for Login.",
            "brief_summary": brief_summary or "Login test suite was created.",
            "test_suite": _mock_suite(),
        }
    gist = brief_summary or (case or message[:160]).replace("\n", " ")
    return {
        "reply": f"(Mock LLM) {message[:300]}",
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
    flow_map: list[dict[str, Any]] | None = None,
    feature_names: list[str] | None = None,
    prd: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    for key, value in (extra or {}).items():
        if value is not None:
            parts.append(
                f"{key}:\n{value if isinstance(value, str) else json.dumps(value, indent=2)}"
            )
    if feature_names:
        parts.append("feature_names:\n" + json.dumps(feature_names))
    if flow_map:
        parts.append("flow_map:\n" + json.dumps(flow_map, indent=2))
    if prd and prd.get("requirements"):
        parts.append("prd:\n" + json.dumps(prd, indent=2))
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
        for chunk in retrieved_context[:100]:
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
        steps.append(
            {"id": step_id.strip(), "label": label.strip(), "actions": actions}
        )
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
        requirement_ids = item.get("requirement_ids")
        grouped[kind].append(
            {
                "kind": kind,
                "title": title.strip(),
                "explanation": explanation.strip(),
                "steps": steps,
                "requirement_ids": [
                    value.strip()
                    for value in requirement_ids
                    if isinstance(value, str) and value.strip()
                ]
                if isinstance(requirement_ids, list)
                else [],
            }
        )
    ordered = [*grouped["success"], *grouped["failure"], *grouped["edge"]]
    ordered = ordered[:40]
    if not ordered:
        return None
    module_key = raw.get("module_key")
    module_name = raw.get("module_name")
    return {
        "feature": feature.strip(),
        "module_key": module_key.strip() if isinstance(module_key, str) else "",
        "module_name": module_name.strip() if isinstance(module_name, str) else "",
        "cases": ordered,
    }


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
    brief = (
        data.get("brief_summary") if isinstance(data.get("brief_summary"), str) else ""
    )
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
        "ready": data.get("ready") is not False,
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
    flow_map: list[dict[str, Any]] | None = None,
    feature_names: list[str] | None = None,
    prd: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    intent: str = "",
    openai_base_url: str = OPENAI_BASE_URL,
) -> dict[str, Any]:
    if mode == "intro" or mock_enabled():
        return _mock_response(mode, message, case, brief_summary, intent)

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
        flow_map=flow_map,
        feature_names=feature_names,
        prd=prd,
        extra=extra,
    )

    content = await json_completion(
        generation_model=generation_model,
        api_key=api_key,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        user_content=user_content,
        openai_base_url=openai_base_url,
        purpose={
            "test_generation": "test suite",
            "feature": "explanation",
            "suite_status": "run status",
        }.get(intent, "reply"),
    )
    return _parse_json_response(content)


async def json_completion(
    *,
    generation_model: str,
    api_key: str,
    system_prompt: str,
    user_content: str,
    openai_base_url: str = OPENAI_BASE_URL,
    purpose: str = "chat",
) -> str:
    """One chat completion that must answer with a JSON object; returns its text."""
    payload = {
        "model": generation_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 1,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    emit(
        "debug",
        "llm",
        f"Calling {generation_model} for {purpose} ({len(user_content):,} characters of context).",
        meta={
            "purpose": purpose,
            "model": generation_model,
            "chars": len(user_content),
        },
    )
    timer = Timer()
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            res = await client.post(
                f"{openai_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        emit(
            "error",
            "llm",
            f"{generation_model} timed out on {purpose}.",
            meta={"purpose": purpose, "model": generation_model},
            duration_ms=timer.ms(),
        )
        raise LlmError(
            "The model took too long to answer. Send the message again."
        ) from exc

    if res.status_code >= 400:
        emit(
            "error",
            "llm",
            f"{generation_model} returned HTTP {res.status_code} on {purpose}.",
            meta={
                "purpose": purpose,
                "model": generation_model,
                "status": res.status_code,
            },
            duration_ms=timer.ms(),
        )
        raise LlmError(f"OpenAI HTTP {res.status_code}: {res.text[:500]}")

    body = res.json()
    usage = body.get("usage") if isinstance(body, dict) else None
    tokens = (
        {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        if isinstance(usage, dict)
        else {}
    )
    emit(
        "info",
        "llm",
        f"{generation_model} answered {purpose} in {timer.ms() / 1000:.1f}s"
        + (
            f" ({tokens['prompt_tokens']} in / {tokens['completion_tokens']} out tokens)."
            if tokens
            else "."
        ),
        meta={"purpose": purpose, "model": generation_model, **tokens},
        duration_ms=timer.ms(),
    )
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("Unexpected OpenAI response shape") from exc

    if not isinstance(content, str):
        raise LlmError("OpenAI message content is not a string")
    return content


def loads_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(
            ln for ln in text.split("\n") if not ln.strip().startswith("```")
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmError(f"LLM returned non-JSON: {text[:300]}") from exc
    if not isinstance(data, dict):
        raise LlmError("LLM JSON must be an object")
    return data


_NUMBERED = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?"
    r"(?P<id>(?:R|FR|NFR|REQ|AC|US)[-_ ]?\d+(?:\.\d+)?)"
    r"(?:\*\*)?\s*[.:)\u2013\u2014-]\s*(?P<text>.*)$",
    re.IGNORECASE,
)


def _prd_title(prd_text: str) -> str:
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip().removeprefix("PRD:").strip()
            if title:
                return title[:80]
    return "PRD"


def numbered_requirements(prd_text: str) -> dict[str, Any] | None:
    """Requirements a PRD already numbers (R1, FR-2, AC3...), read as written.

    The document's own ids and items are kept, so the spec check answers R3 with R3.
    Lines that follow an item, up to a blank line or a heading, continue it.
    """
    requirements: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in prd_text.splitlines():
        stripped = line.strip()
        match = _NUMBERED.match(line)
        if match:
            current = {
                "id": re.sub(r"[\s_]", "", match.group("id")).upper(),
                "text": match.group("text").strip(),
            }
            requirements.append(current)
        elif not stripped or stripped.startswith("#"):
            current = None
        elif current is not None:
            current["text"] = f"{current['text']} {stripped}".strip()
    requirements = [item for item in requirements if item["text"]]
    ids = [item["id"] for item in requirements]
    if len(requirements) < 2 or len(set(ids)) != len(ids):
        return None
    return {"title": _prd_title(prd_text), "requirements": requirements}


def _mock_requirements(prd_text: str) -> dict[str, Any]:
    lines = [
        line.strip(" -*\t") for line in prd_text.splitlines() if line.strip(" -*\t")
    ]
    title = (lines[0] if lines else "PRD").removeprefix("PRD:").strip()[:80] or "PRD"
    body = lines[1:] or lines[:1]
    requirements = [
        {"id": f"R{index}", "text": line}
        for index, line in enumerate(body[:8], start=1)
    ]
    return {"title": title, "requirements": requirements}


async def extract_prd_requirements(
    *, prd_text: str, generation_model: str, api_key: str
) -> dict[str, Any]:
    numbered = numbered_requirements(prd_text)
    if numbered:
        return numbered
    if mock_enabled():
        return _mock_requirements(prd_text)
    data = loads_object(
        await json_completion(
            generation_model=generation_model,
            api_key=api_key,
            system_prompt=PRD_EXTRACT_PROMPT,
            user_content=prd_text,
            purpose="PRD requirements",
        )
    )
    raw = data.get("requirements")
    requirements: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        text = item["text"].strip()
        if text:
            given = item.get("id")
            requirements.append(
                {"id": given.strip() if isinstance(given, str) else "", "text": text}
            )
    if not requirements:
        raise LlmError("I could not find any requirements in that PRD.")
    ids = [item["id"] for item in requirements]
    if not all(ids) or len(set(ids)) != len(ids):
        for index, item in enumerate(requirements, start=1):
            item["id"] = f"R{index}"
    title = data.get("title")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else "PRD",
        "requirements": requirements,
    }


async def check_spec(
    *,
    prd: dict[str, Any],
    case: str,
    flow_map: list[dict[str, Any]],
    retrieved_context: list[dict[str, Any]],
    generation_model: str,
    api_key: str,
) -> dict[str, Any]:
    requirements: list[dict[str, Any]] = prd["requirements"]
    if mock_enabled():
        aligned = [
            {**item, "status": "implemented", "evidence": "", "gap": ""}
            for item in requirements
        ]
        return {
            "reply": (
                f"The implementation is in line with ##{prd['title']}## "
                f"({len(aligned)} requirements checked).\n\n"
                "Shall I generate a test suite that covers them?"
            ),
            "case": f"PRD ##{prd['title']}## was checked against the code. No gaps.",
            "brief_summary": f"Spec check for {prd['title']}: no gaps.",
            "verdict": "aligned",
            "requirements": aligned,
        }
    user_content = _build_user_payload(
        mode="chat",
        message="Check the implementation against these requirements.",
        case=case,
        brief_summary="",
        plans=[],
        cdp_runs=[],
        latest_cdp_steps=None,
        latest_test_suite=None,
        retrieved_context=retrieved_context,
        flow_map=flow_map,
        prd={"title": prd["title"], "requirements": requirements},
    )
    data = loads_object(
        await json_completion(
            generation_model=generation_model,
            api_key=api_key,
            system_prompt=SPEC_CHECK_PROMPT,
            user_content=user_content,
            purpose="spec check",
        )
    )
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise LlmError("LLM JSON must include a reply string")
    judged: dict[str, dict[str, Any]] = {}
    for item in data.get("requirements") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            judged[item["id"]] = item
    results: list[dict[str, Any]] = []
    for item in requirements:
        found = judged.get(item["id"], {})
        status = found.get("status")
        results.append(
            {
                "id": item["id"],
                "text": item["text"],
                "status": status
                if status in {"implemented", "partial", "missing"}
                else "missing",
                "evidence": str(found.get("evidence") or "").strip(),
                "gap": str(found.get("gap") or "").strip(),
            }
        )
    verdict = (
        "aligned" if all(r["status"] == "implemented" for r in results) else "gaps"
    )
    return {
        "reply": reply.strip(),
        "case": str(data.get("case") or "").strip(),
        "brief_summary": str(data.get("brief_summary") or "").strip(),
        "verdict": verdict,
        "requirements": results,
    }


async def write_report_analysis(
    *,
    context: dict[str, Any],
    generation_model: str,
    api_key: str,
) -> dict[str, Any]:
    """Prose for the report; the caller validates it and fills anything missing."""
    if mock_enabled() or not generation_model or not api_key:
        # Empty prose: the report falls back to the flow map and the run data.
        return {}
    return loads_object(
        await json_completion(
            generation_model=generation_model,
            api_key=api_key,
            system_prompt=REPORT_PROMPT,
            user_content=json.dumps(context, indent=2),
            purpose="report analysis",
        )
    )
