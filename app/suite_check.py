"""Is a saved suite still valid for today's code?

Layer 1 is exact: every route, selector, and text the suite's steps rely on is looked
up in the current code. Layer 2 asks the model, with the current code of the flow and
the suite's steps, which cases no longer match and what the flow now does that no case
covers. Layer 2 runs only when the fingerprint of the flow's files changed since the
last check; otherwise the stored verdict is reused.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from app.flow_map import _match_route, _route_pattern, dependency_chunks, public_node
from app.log_relay import Timer, emit
from app.storage import StoredChunkRecord

_TESTID = re.compile(r"data-testid\s*=\s*[\"']?([^\"'\]\s]+)")
_HAS_TEXT = re.compile(r":has-text\(\s*[\"'](.+?)[\"']\s*\)")
_TEXT_EQ = re.compile(r"^text\s*=\s*[\"']?(.+?)[\"']?$")
_ATTR = re.compile(
    r"\[(name|placeholder|aria-label|id|for)\s*=\s*[\"']([^\"']+)[\"']\]"
)
_HASH_ID = re.compile(r"#([A-Za-z_][\w-]*)")
_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")
_MAX_CODE_CHUNKS = 60
_TEMPLATE = re.compile(r"`([^`]*\$\{[^`]*)`")
_JSX_TEXT = re.compile(r">([^<>]*\{[^<>]*)<")
_INTERPOLATION = re.compile(r"\$\{[^}]*\}|\{[^{}]*\}")
_MIN_LITERAL = 3

CHECK_PROMPT = """You are checking whether a saved CDP test suite still fits the current code of a web app.

You are given the suite's cases (title, kind, and steps), problems an exact search already found (a selector, text, or route a step uses that the search could not find), confirmed_in_code (per case, the texts and selectors the exact search did find in the code, including text built at runtime; these are present, so never call a case stale because of one of them), feature_screen (the screen this suite tests), passed_through (screens the cases only pass through on the way), and the current code of those screens.

Decide:
- stale_cases: cases that would fail or no longer test what their title says against this code. For each, give a short reason a developer can act on, naming the control or text, for example: the submit button now says "Log in", not "Sign in". The exact search can be wrong: text built at runtime from variables still counts as present (code `Hello, ${user.name}` renders "Hello, Tursor" for a user named Tursor), and so does text from data such as a list of items. Drop a listed problem when the code still produces that text or control.
- uncovered: behaviour of feature_screen only that no case covers (a new field, button, error message, or path on that screen). Never list behaviour of passed_through screens. Short phrases. Leave out anything minor.
- summary: one sentence.

Judge only from the code given. Do not invent controls.

Respond with one JSON object and no markdown fences:
{ "stale_cases": [ { "title": "", "reason": "" } ], "uncovered": [], "summary": "" }
"""


def _norm(text: str) -> str:
    return " ".join(text.replace("…", "...").split()).lower()


def _attr_present(code: str, attr: str, value: str) -> bool:
    pattern = rf"{re.escape(attr)}\s*=\s*\{{?\s*[\"'`]{re.escape(value)}[\"'`]"
    return re.search(pattern, code) is not None


def dynamic_patterns(code: str) -> list[re.Pattern[str]]:
    """Template strings and JSX text with {expressions}, as patterns for rendered text.

    `Hello, ${user?.name}` and <h1>Hello, {name}</h1> both match "Hello, Tursor".
    """
    patterns: list[re.Pattern[str]] = []
    for raw in [*_TEMPLATE.findall(code), *_JSX_TEXT.findall(code)]:
        parts = _INTERPOLATION.split(raw)
        if len("".join(parts).strip()) < _MIN_LITERAL:
            continue
        body = ".+?".join(re.escape(_norm(part)) for part in parts)
        try:
            patterns.append(re.compile(body))
        except re.error:
            continue
    return patterns


def text_present(text: str, code_lower: str, patterns: list[re.Pattern[str]]) -> bool:
    wanted = _norm(text)
    if wanted in code_lower:
        return True
    return any(pattern.fullmatch(wanted) for pattern in patterns)


def selector_found(
    selector: str,
    code: str,
    code_lower: str,
    patterns: list[re.Pattern[str]] | None = None,
) -> bool | None:
    """True or False when the selector can be checked against the code; None when not."""
    checks: list[bool] = []
    for value in _TESTID.findall(selector):
        checks.append(_attr_present(code, "data-testid", value))
    dynamic = patterns or []
    for value in _HAS_TEXT.findall(selector):
        checks.append(text_present(value, code_lower, dynamic))
    text_eq = _TEXT_EQ.match(selector.strip())
    if text_eq:
        checks.append(text_present(text_eq.group(1), code_lower, dynamic))
    for attr, value in _ATTR.findall(selector):
        if attr in {"placeholder", "aria-label"}:
            checks.append(_norm(value) in code_lower)
        else:
            checks.append(
                _attr_present(code, "htmlFor" if attr == "for" else attr, value)
                or _attr_present(code, attr, value)
            )
    stripped = re.sub(r"\[[^\]]*\]|:has-text\([^)]*\)", "", selector)
    for value in _HASH_ID.findall(stripped):
        checks.append(_attr_present(code, "id", value))
    for value in _CLASS.findall(stripped):
        checks.append(re.search(rf"\b{re.escape(value)}\b", code) is not None)
    if not checks:
        return None
    return all(checks)


def exact_problems(
    case: dict[str, Any],
    code: str,
    routes: list[str],
) -> list[str]:
    """What this case relies on that the current code no longer has."""
    return exact_findings(case, code, routes)[0]


def exact_findings(
    case: dict[str, Any],
    code: str,
    routes: list[str],
) -> tuple[list[str], list[str]]:
    """Problems as above, and the texts and selectors the search found in the code."""
    code_lower = _norm(code)
    patterns = [(route, _route_pattern(route)) for route in routes]
    dynamic = dynamic_patterns(code)
    problems: list[str] = []
    found: list[str] = []
    for step in case.get("steps") or []:
        label = str(step.get("label") or step.get("id") or "a step")
        for action in step.get("actions") or []:
            kind = action.get("type")
            if kind == "navigate":
                path = str(action.get("path") or "/")
                if (
                    not path.startswith("http")
                    and routes
                    and not _match_route(path, patterns)
                ):
                    problems.append(
                        f'Step "{label}" opens {path}, which is no longer a route.'
                    )
            elif kind == "waitForPath":
                fragment = str(action.get("pathIncludes") or "")
                if (
                    fragment
                    and routes
                    and not any(fragment in route for route in routes)
                ):
                    problems.append(
                        f'Step "{label}" waits for {fragment}, which no route contains.'
                    )
            elif kind == "waitForText":
                text = str(action.get("text") or "")
                if text and not text_present(text, code_lower, dynamic):
                    problems.append(
                        f'Step "{label}" waits for the text "{text}", which the code no longer contains.'
                    )
                elif text:
                    found.append(text)
            selectors = [s for s in action.get("selectors") or [] if isinstance(s, str)]
            if selectors:
                results = [
                    selector_found(s, code, code_lower, dynamic) for s in selectors
                ]
                checkable = [r for r in results if r is not None]
                if checkable and not any(checkable) and None not in results:
                    problems.append(
                        f'Step "{label}" uses {selectors[0]}, which the code no longer renders.'
                    )
                found += [s for s, ok in zip(selectors, results, strict=True) if ok]
    return problems, list(dict.fromkeys(found))


# Apostrophes are left out: "isn't" would pair with the next one.
_QUOTED = re.compile(r"[\"“”`]([^\"“”`]{2,})[\"“”`]")


def contradicts_code(reason: str, confirmed: list[str]) -> bool:
    """True when a model's stale reason is only about things the exact search found.

    The reason quotes what it says is missing; when every quoted item was found in the
    code, or (without quotes) it names a found text, the reason is wrong.
    """
    if not confirmed:
        return False
    known = {_norm(item) for item in confirmed}
    quoted = [_norm(item) for item in _QUOTED.findall(reason)]
    if quoted:
        return all(item in known for item in quoted)
    text = _norm(reason)
    return any(len(item) >= 4 and item in text for item in known)


def _involved_nodes(
    suite: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The suite's own screen, every screen its steps open, and the screens on the way."""
    by_route = {str(node.get("route")): node for node in nodes}
    patterns = [(route, _route_pattern(route)) for route in by_route]
    keys: list[str] = []
    module_key = str(suite.get("module_key") or "")
    if module_key in by_route:
        keys.append(module_key)
        keys += [str(hop.get("from")) for hop in by_route[module_key].get("entry", [])]
    for case in suite.get("cases") or []:
        for step in case.get("steps") or []:
            for action in step.get("actions") or []:
                target = action.get("path") or action.get("pathIncludes")
                if isinstance(target, str):
                    route = _match_route(target, patterns) or (
                        target if target in by_route else None
                    )
                    if route:
                        keys.append(route)
    ordered = list(dict.fromkeys(key for key in keys if key in by_route))
    return [by_route[key] for key in ordered]


def suite_fingerprint(
    suite: dict[str, Any], nodes: list[dict[str, Any]], hashes: dict[str, str]
) -> str:
    """Fingerprint of the files behind a suite, as a later check will compute it."""
    return fingerprint(_involved_nodes(suite, nodes), hashes)


def accounts_used(suite: dict[str, Any]) -> dict[str, Any]:
    """Values a saved suite already types in, so they are not asked for again.

    Emails and user names are shown; a password is only noted as saved.
    """
    identities: list[str] = []
    has_password = False
    for case in suite.get("cases") or []:
        if case.get("kind") != "success":
            continue
        for step in case.get("steps") or []:
            for action in step.get("actions") or []:
                value = action.get("value")
                if (
                    action.get("type") != "fill"
                    or not isinstance(value, str)
                    or not value
                ):
                    continue
                where = " ".join(
                    [
                        str(step.get("label") or ""),
                        *map(str, action.get("selectors") or []),
                    ]
                ).lower()
                if "password" in where:
                    has_password = True
                elif ("email" in where or "user" in where) and value not in identities:
                    identities.append(value)
    return {"accounts": identities[:5], "password_saved": has_password}


def fingerprint(involved: list[dict[str, Any]], hashes: dict[str, str]) -> str:
    files = sorted({path for node in involved for path in node.get("dependencies", [])})
    lines = "\n".join(f"{path}:{hashes.get(path, '')}" for path in files)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _compact_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "title": case.get("title"),
            "kind": case.get("kind"),
            "steps": [
                {"label": step.get("label"), "actions": step.get("actions")}
                for step in case.get("steps") or []
            ],
        }
        for case in suite.get("cases") or []
    ]


async def check_suite(
    *,
    suite: dict[str, Any],
    nodes: list[dict[str, Any]],
    records: list[StoredChunkRecord],
    code: str,
    hashes: dict[str, str],
    generation_model: str,
    api_key: str,
) -> dict[str, Any]:
    """Verdict for one saved suite: valid, stale cases with reasons, and gaps."""
    from app.llm_service import LlmError, json_completion, loads_object, mock_enabled

    involved = _involved_nodes(suite, nodes)
    current = fingerprint(involved, hashes)
    cached = suite.get("check_result")
    if suite.get("checked_fingerprint") == current and isinstance(cached, dict):
        emit(
            "info",
            "suite",
            f"v{suite.get('version')} was checked before and its files have not changed; "
            "reusing that verdict.",
            meta={"suite_id": suite.get("suite_id"), "valid": cached.get("valid")},
        )
        return {**cached, "fingerprint": current, "reused": True}

    timer = Timer()
    routes = [str(node.get("route")) for node in nodes]
    exact: dict[str, list[str]] = {}
    confirmed: dict[str, list[str]] = {}
    for case in suite.get("cases") or []:
        problems, found = exact_findings(case, code, routes)
        title = str(case.get("title"))
        if problems:
            exact[title] = problems
        if found:
            confirmed[title] = found

    stale: list[dict[str, str]] = [
        {"title": title, "reason": problems[0]} for title, problems in exact.items()
    ]
    uncovered: list[str] = []
    summary = ""
    if not mock_enabled() and generation_model and api_key:
        chunks = dependency_chunks(records, [public_node(node) for node in involved])
        module_key = str(suite.get("module_key") or "")
        payload = {
            "cases": _compact_cases(suite),
            "exact_problems": exact,
            "confirmed_in_code": confirmed,
            "feature_screen": next(
                (public_node(n) for n in involved if n.get("route") == module_key), None
            ),
            "passed_through": [
                public_node(n) for n in involved if n.get("route") != module_key
            ],
            "code": [
                f"--- {chunk['path']}:{chunk['start_line']}-{chunk['end_line']} ---\n"
                f"{chunk['content']}"
                for chunk in chunks[:_MAX_CODE_CHUNKS]
            ],
        }
        try:
            data = loads_object(
                await json_completion(
                    generation_model=generation_model,
                    api_key=api_key,
                    system_prompt=CHECK_PROMPT,
                    user_content=json.dumps(payload, indent=2),
                    purpose="suite check",
                )
            )
            titles = {str(case.get("title")) for case in suite.get("cases") or []}
            judged: list[dict[str, str]] = []
            for item in data.get("stale_cases") or []:
                if not isinstance(item, dict) or str(item.get("title")) not in titles:
                    continue
                title, reason = str(item.get("title")), str(item.get("reason") or "")
                # The search found what this reason calls missing: the search wins.
                if contradicts_code(reason, confirmed.get(title, [])):
                    emit(
                        "info",
                        "suite",
                        f"Dropped a stale verdict for “{title}”: the code contains "
                        f"what it says is missing ({reason[:160]}).",
                    )
                    continue
                judged.append({"title": title, "reason": reason})
            if judged or not exact:
                stale = judged
            uncovered = [
                item.strip()
                for item in data.get("uncovered") or []
                if isinstance(item, str) and item.strip()
            ][:8]
            summary = str(data.get("summary") or "").strip()
        except LlmError as exc:
            emit(
                "warn",
                "suite",
                f"Model review of the suite failed; using the exact check only. {exc}",
            )

    result = {
        "suite_id": suite.get("suite_id"),
        "valid": not stale,
        "stale_cases": stale,
        "uncovered": uncovered,
        "summary": summary,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    emit(
        "success" if result["valid"] else "warn",
        "suite",
        f"Checked v{suite.get('version')} against the current code: "
        + (
            "still valid"
            if result["valid"]
            else f"{len(stale)} of {len(suite.get('cases') or [])} case(s) no longer match"
        )
        + (f"; {len(uncovered)} new behaviour(s) not covered" if uncovered else "")
        + ".",
        meta={
            "suite_id": suite.get("suite_id"),
            "stale": [item["title"] for item in stale],
        },
        duration_ms=timer.ms(),
    )
    return {**result, "fingerprint": current, "reused": False}
