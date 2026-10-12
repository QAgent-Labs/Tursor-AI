"""Rewrite suite selectors so a class uses the tag the source actually renders."""

from __future__ import annotations

import re
from typing import Any

_CLASS_ATTR = re.compile(
    r"<([A-Za-z][\w.]*)\b[^>]*?\bclassName\s*=\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|\{`([^`]*)`\}|\{\"([^\"]*)\"\}|\{'([^']*)'\})",
    re.DOTALL,
)
_CLASS_TOKEN = re.compile(r"[A-Za-z_][\w-]*")
_INTERP = re.compile(r"\$\{[^}]*\}")
_TAG_CLASS = re.compile(r"(?<![\w-])([a-z][\w-]*)\.([A-Za-z_][\w-]*)")


def class_owners(sources: list[str]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for source in sources:
        for match in _CLASS_ATTR.finditer(source):
            tag = match.group(1).split(".")[-1]
            if tag[:1].isupper():
                continue
            raw = next(group for group in match.groups()[1:] if group is not None)
            cleaned = _INTERP.sub(" ", raw)
            for token in _CLASS_TOKEN.findall(cleaned):
                owners.setdefault(token, set()).add(tag)
    return owners


def ground_selector(selector: str, owners: dict[str, set[str]]) -> str | None:
    invented = False

    def replace_tag(match: re.Match[str]) -> str:
        nonlocal invented
        tag, class_name = match.group(1), match.group(2)
        tags = owners.get(class_name)
        if not tags:
            invented = True
            return match.group(0)
        if tag in tags or len(tags) != 1:
            return match.group(0)
        return f"{next(iter(tags))}.{class_name}"

    rewritten = _TAG_CLASS.sub(replace_tag, selector)
    if invented:
        return None
    return rewritten


def ground_suite(suite: dict[str, Any] | None, sources: list[str]) -> dict[str, Any] | None:
    if not suite or not sources:
        return suite
    owners = class_owners(sources)
    cases: list[dict[str, Any]] = []
    for case in suite.get("cases", []):
        if not isinstance(case, dict):
            continue
        steps: list[dict[str, Any]] = []
        for step in case.get("steps", []):
            if not isinstance(step, dict):
                continue
            actions: list[dict[str, Any]] = []
            for action in step.get("actions", []):
                if not isinstance(action, dict):
                    continue
                selectors = action.get("selectors")
                if not isinstance(selectors, list):
                    actions.append(action)
                    continue
                grounded = [
                    next_selector
                    for selector in selectors
                    if isinstance(selector, str)
                    for next_selector in [ground_selector(selector, owners)]
                    if next_selector
                ]
                if not grounded:
                    continue
                actions.append({**action, "selectors": grounded})
            if actions:
                steps.append({**step, "actions": actions})
        if steps:
            cases.append({**case, "steps": steps})
    if not cases:
        return None
    return {**suite, "cases": cases}
