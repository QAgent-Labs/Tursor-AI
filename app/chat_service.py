from __future__ import annotations

from pathlib import Path
from typing import Any

from app.flow_map import (
    dependency_chunks,
    file_hashes,
    flow_nodes,
    load_flow_map,
    public_node,
    select_nodes,
)
from app.intent_router import (
    CHANGE_SUITE,
    CONFIRM,
    DECLINE,
    DECLINED,
    FEATURE,
    GENERAL_QA,
    OUT_OF_SCOPE,
    OUT_OF_SCOPE_REPLY,
    PENDING_ACTIONS,
    PENDING_COLLECT,
    PENDING_COLLECT_GENERATE,
    PENDING_COLLECT_NEW_VERSION,
    PENDING_GENERATE,
    PENDING_NEW_VERSION,
    PENDING_REUSE,
    PROVIDE_DATA,
    SPEC_CHECK,
    SUITE_REUSED,
    SUITE_STATUS,
    TEST_GENERATION,
    IntentDecision,
    classify_intent,
)
from app.llm_service import (
    LlmError,
    check_spec,
    complete_chat,
    extract_prd_requirements,
    mock_enabled,
)
from app.log_relay import Timer, emit
from app.prompts import (
    EXPLAIN_PROMPT,
    GENERAL_CHAT_PROMPT,
    SUITE_STATUS_PROMPT,
    SYSTEM_PROMPT,
)
from app.rag_service import ensure_embeddings_exist, search_chunks, search_workspace
from app.selector_grounding import ground_suite
from app.storage import StoredChunkRecord, load_stored_chunks
from app.suite_check import accounts_used, check_suite, suite_fingerprint
from app.tursor_config import load_tursor_config

_FLOW_NODES = 3
_REQUIREMENT_TOP_K = 8


def _with_dependency_chunks(
    records: list[Any],
    nodes: list[dict[str, Any]],
    retrieved: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    required = dependency_chunks(records, nodes)
    seen = {(item.get("path"), item.get("start_line")) for item in required}
    merged = list(required)
    for item in retrieved:
        key = (item.get("path"), item.get("start_line"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:limit]


def _retrieval_query(message: str, case: str) -> str:
    parts = [message.strip()]
    recorded = case.strip()
    if recorded:
        parts.append(recorded[:2000])
    return "\n".join(part for part in parts if part)


def _prompt_nodes(
    named: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Screens the model picked come first, then the closest by meaning."""
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in [*(public_node(item) for item in named), *selected]:
        key = str(node.get("module_key", ""))
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
    return nodes[:_FLOW_NODES]


def _matching_suite(
    existing_suites: list[dict[str, Any]],
    decision: IntentDecision,
    prompt_nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The suite the model named, else one on a screen it picked, else on the closest."""
    if decision.suite_id:
        found = next(
            (s for s in existing_suites if s.get("suite_id") == decision.suite_id), None
        )
        if found:
            return found
    keys = [*decision.screens, *(str(n.get("module_key")) for n in prompt_nodes[:1])]
    for key in keys:
        for suite in existing_suites:
            if suite.get("module_key") == key:
                return suite
    return None


def _suite_name(suite: dict[str, Any]) -> str:
    return str(suite.get("module_name") or suite.get("feature") or "this feature")


def _feature_path(key: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The feature's screen and the screens a person passes on the way to it."""
    by_route = {str(node.get("route")): node for node in nodes}
    feature = by_route.get(key)
    if not feature:
        return []
    on_the_way = [
        by_route[str(hop.get("from"))]
        for hop in feature.get("entry", [])
        if str(hop.get("from")) in by_route
    ]
    return [*on_the_way, feature]


def _suite_overview(suite: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """What the explain step is told about a saved suite."""
    cases = suite.get("cases") or []
    kinds = [str(case.get("kind")) for case in cases]
    statuses = [str(case.get("last_status") or "") for case in cases]
    return {
        "name": _suite_name(suite),
        "version": suite.get("version", 1),
        "created_at": str(suite.get("created_at") or "")[:10],
        "cases": len(cases),
        "success": kinds.count("success"),
        "failure": kinds.count("failure"),
        "edge": kinds.count("edge"),
        "last_runs": {
            "passed": statuses.count("passed"),
            "failed": statuses.count("failure"),
            "not_run": statuses.count(""),
        },
        "check": {
            "valid": check.get("valid", True),
            "stale_cases": check.get("stale_cases", []),
            "uncovered": check.get("uncovered", []),
        },
        # Values the saved suite already types in; reused instead of asking again.
        "accounts_used": accounts_used(suite),
    }


def _run_counts(suite: dict[str, Any]) -> dict[str, int]:
    statuses = [str(case.get("last_status") or "") for case in suite.get("cases") or []]
    return {
        "total": len(statuses),
        "passed": statuses.count("passed"),
        "failed": statuses.count("failure"),
        "not_run": statuses.count(""),
    }


def suite_status_line(suite: dict[str, Any]) -> str:
    """The saved suite in one paragraph, from the data, so the counts are always right."""
    kinds = [str(case.get("kind")) for case in suite.get("cases") or []]
    counts = _run_counts(suite)
    if counts["not_run"] == counts["total"]:
        runs = "None of its cases has been run yet."
    else:
        runs = f"Latest runs: {counts['passed']} passed, {counts['failed']} failed" + (
            f", {counts['not_run']} not run." if counts["not_run"] else "."
        )
    return (
        f"This feature already has a test suite: **{_suite_name(suite)} "
        f"v{suite.get('version', 1)}** with {counts['total']} cases "
        f"({kinds.count('success')} success, {kinds.count('failure')} failure, "
        f"{kinds.count('edge')} edge). {runs}"
    )


def _run_facts(suite: dict[str, Any]) -> dict[str, Any]:
    """What a question about a suite's runs is answered from."""
    return {
        "name": _suite_name(suite),
        "version": suite.get("version", 1),
        "suite_id": suite.get("suite_id"),
        "counts": _run_counts(suite),
        "cases": [
            {
                "title": case.get("title"),
                "kind": case.get("kind"),
                "latest_run": case.get("last_run"),
            }
            for case in suite.get("cases") or []
        ],
    }


def _status_fallback(facts: list[dict[str, Any]]) -> str:
    """Without the model: the counts and every failed case, straight from the data."""
    blocks: list[str] = []
    for suite in facts:
        counts = suite["counts"]
        lines = [
            f"**{suite['name']} v{suite['version']}**: {counts['passed']} passed, "
            f"{counts['failed']} failed, {counts['not_run']} not run."
        ]
        for case in suite["cases"]:
            run = case.get("latest_run") or {}
            if run.get("status") == "failure":
                lines.append(
                    f"- **{case['title']}** failed at {run.get('failed_step') or 'a step'}"
                    f" (`{run.get('run_id') or '-'}`)"
                )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "No saved test suite has been run yet."


def _plain(
    reply: str,
    case: str,
    brief_summary: str,
    intent: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "reply": reply,
        "case": case,
        "brief_summary": brief_summary,
        "test_suite": None,
        "retrieved_chunk_count": 0,
        "intent": intent,
        "focus_paths": [],
        **extra,
    }


def _code_text(records: list[StoredChunkRecord]) -> str:
    return "\n".join(record.text for record in records)


async def run_chat_completion(
    *,
    workspace_path: str,
    message: str,
    generation_model: str,
    api_key: str,
    mode: str = "chat",
    case: str = "",
    brief_summary: str = "",
    plans: list[dict[str, Any]] | None = None,
    cdp_runs: list[dict[str, Any]] | None = None,
    latest_cdp_steps: list[dict[str, Any]] | None = None,
    latest_test_suite: dict[str, Any] | None = None,
    rag_top_k: int = 100,
    prior_intent: str = "",
    focus_paths: list[str] | None = None,
    existing_suites: list[dict[str, Any]] | None = None,
    pending_action: str = "",
    pending_suite_id: str = "",
    active_suite_id: str = "",
    prd: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One chat turn: route it, then explain, generate, reuse, or answer."""
    cfg = load_tursor_config(Path(workspace_path))
    ensure_embeddings_exist(cfg)
    common: dict[str, Any] = {
        "generation_model": generation_model,
        "api_key": api_key,
        "mode": mode,
        "message": message,
        "case": case,
        "brief_summary": brief_summary,
        "plans": plans,
        "cdp_runs": cdp_runs,
        "latest_cdp_steps": latest_cdp_steps,
        "latest_test_suite": latest_test_suite,
        "prd": prd,
    }
    if mode == "intro":
        return await _complete(
            **common,
            retrieved=[],
            flow_map=[],
            intent="",
            focus_paths=[],
            system_prompt=SYSTEM_PROMPT,
            allow_suite=False,
        )

    flow = load_flow_map(cfg.embeddings_path)
    nodes = flow_nodes(flow)
    names = [str(node["name"]) for node in nodes if isinstance(node.get("name"), str)]
    suites = existing_suites or []
    pending = pending_action if pending_action in PENDING_ACTIONS else ""

    timer = Timer()
    try:
        decision = await classify_intent(
            message=message,
            case=case,
            pending_action=pending,
            pending_suite_id=pending_suite_id,
            active_suite_id=active_suite_id,
            nodes=nodes,
            suites=suites,
            generation_model=generation_model,
            api_key=api_key,
        )
    except LlmError as exc:
        # Without a decision, explain: it never generates, so it is always safe.
        decision = IntentDecision(FEATURE, [], "", f"classifier unavailable: {exc}")
        emit("warn", "intent", "Could not classify the message; explaining instead.")
    emit(
        "info",
        "intent",
        f"Intent: {decision.intent}"
        + (f" · screens {', '.join(decision.screens)}" if decision.screens else "")
        + (f" · answering “{pending}”" if decision.intent in {CONFIRM, DECLINE} else "")
        + (f" — {decision.reason}" if decision.reason else ""),
        meta={
            "intent": decision.intent,
            "screens": decision.screens,
            "suite_id": decision.suite_id,
            "pending_action": pending,
            "reason": decision.reason,
        },
        duration_ms=timer.ms(),
    )

    if decision.intent == OUT_OF_SCOPE:
        return _plain(OUT_OF_SCOPE_REPLY, case, brief_summary, OUT_OF_SCOPE)

    if decision.intent == SPEC_CHECK:
        result = await _spec_check(
            cfg_path=workspace_path,
            prd_text=message,
            case=case,
            brief_summary=brief_summary,
            flow=flow,
            generation_model=generation_model,
            api_key=api_key,
            rag_top_k=rag_top_k,
        )
        # The spec reply ends by offering a suite for the implemented requirements.
        return {
            **result,
            "pending_action": PENDING_GENERATE if result.get("prd") else "",
        }

    if decision.intent == GENERAL_QA:
        return await _complete(
            **common,
            retrieved=[],
            flow_map=[],
            feature_names=names,
            intent=GENERAL_QA,
            focus_paths=[],
            system_prompt=GENERAL_CHAT_PROMPT,
            allow_suite=False,
        )

    pending_suite = next(
        (s for s in suites if s.get("suite_id") == pending_suite_id), None
    )

    if decision.intent == SUITE_STATUS:
        target = (
            _matching_suite(suites, decision, [])
            or pending_suite
            or next((s for s in suites if s.get("suite_id") == active_suite_id), None)
        )
        facts = [_run_facts(s) for s in ([target] if target else suites[:10])]
        if mock_enabled():
            result = _plain(_status_fallback(facts), case, brief_summary, SUITE_STATUS)
        else:
            result = await _complete(
                **common,
                retrieved=[],
                flow_map=[],
                extra={"suites": facts},
                intent=SUITE_STATUS,
                focus_paths=[],
                system_prompt=SUITE_STATUS_PROMPT,
                allow_suite=False,
            )
        emit("info", "chat", f"Answered from the run data of {len(facts)} suite(s).")
        # A question in between does not cancel the question Tursor asked before.
        return {
            **result,
            "pending_action": pending,
            "pending_suite_id": pending_suite_id,
        }

    if decision.intent == DECLINE:
        if pending == PENDING_REUSE and pending_suite:
            name = _suite_name(pending_suite)
            version = int(pending_suite.get("version", 1))
            emit(
                "info",
                "suite",
                f"Reuse of {name} v{version} declined; offering v{version + 1}.",
            )
            return _plain(
                f"Okay, I won't reuse **{name} v{version}**.\n\n"
                f"**Shall I generate v{version + 1} instead?** Tell me if there is "
                "anything specific it should cover.",
                case,
                brief_summary,
                DECLINED,
                pending_action=PENDING_NEW_VERSION,
                pending_suite_id=pending_suite_id,
            )
        emit("info", "suite", "Declined; nothing generated.")
        return _plain(
            "Okay, I won't generate anything. Tell me what you'd like to change, "
            "or which flow to look at next.",
            case,
            brief_summary,
            DECLINED,
        )

    if decision.intent == CONFIRM and pending == PENDING_REUSE and pending_suite:
        name = _suite_name(pending_suite)
        version = pending_suite.get("version", 1)
        emit("success", "suite", f"Reusing {name} v{version} as it is.")
        return _plain(
            f"Reusing **{name} v{version}** as it is — it still matches the current code.",
            f"{case.strip()}\n\nReusing the {name} suite, version {version}.".strip(),
            brief_summary or f"Reused the {name} test suite.",
            SUITE_REUSED,
            reuse_suite_id=pending_suite_id,
        )

    records = load_stored_chunks(cfg.embeddings_path)
    hashes = file_hashes(cfg.embeddings_path)
    named = [
        node for key in decision.screens for node in nodes if node.get("route") == key
    ]
    to_new_version = pending in {PENDING_NEW_VERSION, PENDING_COLLECT_NEW_VERSION}
    base_check = (
        pending_suite.get("check_result") if to_new_version and pending_suite else None
    )

    if decision.intent == PROVIDE_DATA:
        # The user answered the request for values. Giving them is the go-ahead:
        # once nothing is missing, generate in this same turn without asking again.
        collected = await _collect_values(
            common=common,
            workspace_path=workspace_path,
            flow=flow,
            nodes=nodes,
            named=named,
            records=records,
            feature_key=str((pending_suite or {}).get("module_key") or ""),
            pending=pending,
            pending_suite_id=pending_suite_id,
            rag_top_k=rag_top_k,
        )
        if collected.get("pending_action") in PENDING_COLLECT:
            return collected
        common = {
            **common,
            "case": collected.get("case") or common["case"],
            "brief_summary": collected.get("brief_summary") or common["brief_summary"],
        }

    generate = decision.intent in {CHANGE_SUITE, PROVIDE_DATA} or (
        decision.intent == CONFIRM
        and pending in {PENDING_GENERATE, PENDING_NEW_VERSION}
    )
    if generate:
        return await _generate(
            common=common,
            workspace_path=workspace_path,
            flow=flow,
            nodes=nodes,
            named=named,
            records=records,
            latest_test_suite=latest_test_suite,
            suite_check=base_check,
            hashes=hashes,
            rag_top_k=rag_top_k,
        )

    return await _explain(
        common=common,
        workspace_path=workspace_path,
        flow=flow,
        nodes=nodes,
        named=named,
        records=records,
        suites=suites,
        decision=decision,
        hashes=hashes,
        follow_up=prior_intent == FEATURE,
        rag_top_k=rag_top_k,
    )


def _retrieve(
    *,
    workspace_path: str,
    message: str,
    case: str,
    flow: dict[str, Any] | None,
    named: list[dict[str, Any]],
    records: list[StoredChunkRecord],
    rag_top_k: int,
    only_named: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Code for this turn. With only_named, the screens are exactly the ones given."""
    query = _retrieval_query(message, case)
    timer = Timer()
    retrieved = (
        search_workspace(workspace_path, query, top_k=rag_top_k) if query else []
    )
    if only_named and named:
        prompt_nodes = [public_node(node) for node in named]
    else:
        selected = select_nodes(flow, query, top_k=_FLOW_NODES) if query else []
        prompt_nodes = _prompt_nodes(named, selected)
    retrieved = _with_dependency_chunks(records, prompt_nodes, retrieved, rag_top_k)
    top_files = list(dict.fromkeys(str(item.get("path")) for item in retrieved))[:5]
    emit(
        "info",
        "retrieval",
        f"Retrieved {len(retrieved)} code chunks; screens "
        + (", ".join(str(n["module_key"]) for n in prompt_nodes) or "none")
        + ".",
        meta={
            "chunks": len(retrieved),
            "screens": [n["module_key"] for n in prompt_nodes],
            "top_files": top_files,
        },
        duration_ms=timer.ms(),
    )
    return retrieved, prompt_nodes


async def _explain(
    *,
    common: dict[str, Any],
    workspace_path: str,
    flow: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    named: list[dict[str, Any]],
    records: list[StoredChunkRecord],
    suites: list[dict[str, Any]],
    decision: IntentDecision,
    hashes: dict[str, str],
    follow_up: bool,
    rag_top_k: int,
) -> dict[str, Any]:
    """Explain the flow, check a saved suite if there is one, and ask before acting."""
    existing = _matching_suite(suites, decision, [])
    feature_key = str((existing or {}).get("module_key") or "")
    if not feature_key and decision.screens:
        feature_key = decision.screens[0]
    if not feature_key:
        query = _retrieval_query(common["message"], common["case"])
        closest = select_nodes(flow, query, top_k=1) if query else []
        feature_key = str(closest[0]["module_key"]) if closest else ""
    if not existing and feature_key:
        existing = next((s for s in suites if s.get("module_key") == feature_key), None)
    path_nodes = _feature_path(feature_key, nodes) or named

    retrieved, prompt_nodes = _retrieve(
        workspace_path=workspace_path,
        message=common["message"],
        case=common["case"],
        flow=flow,
        named=path_nodes,
        records=records,
        rag_top_k=rag_top_k,
        only_named=True,
    )
    check: dict[str, Any] | None = None
    if existing:
        check = await check_suite(
            suite=existing,
            nodes=nodes,
            records=records,
            code=_code_text(records),
            hashes=hashes,
            generation_model=common["generation_model"],
            api_key=common["api_key"],
        )
        next_step = "ask_reuse" if check.get("valid") else "ask_new_version"
        pending = PENDING_REUSE if check.get("valid") else PENDING_NEW_VERSION
    else:
        next_step = "ask_generate"
        pending = PENDING_GENERATE

    result = await _complete(
        **common,
        retrieved=retrieved,
        flow_map=prompt_nodes,
        records=records,
        extra={
            "existing_suite": _suite_overview(existing, check)
            if existing and check
            else None,
            "next_step": next_step,
            "follow_up": follow_up,
        },
        intent=FEATURE,
        focus_paths=_dependency_paths(prompt_nodes),
        system_prompt=EXPLAIN_PROMPT,
        allow_suite=False,
    )
    if existing and not follow_up and result.get("reply"):
        result["reply"] = f"{suite_status_line(existing)}\n\n{result['reply']}"
    # Values still missing: ask for them; supplying them later starts generation.
    ready = result.get("ready", True) is not False
    if not ready and pending == PENDING_GENERATE:
        pending = PENDING_COLLECT_GENERATE
    elif not ready and pending == PENDING_NEW_VERSION:
        pending = PENDING_COLLECT_NEW_VERSION
    emit(
        "info",
        "chat",
        "Explained the flow; "
        + {
            PENDING_GENERATE: "asking whether to generate the suite.",
            PENDING_REUSE: "asking whether to reuse the saved version.",
            PENDING_NEW_VERSION: "asking whether to generate the next version.",
            PENDING_COLLECT_GENERATE: "asking for the values the tests need.",
            PENDING_COLLECT_NEW_VERSION: "asking for the values the tests need.",
        }[pending],
    )
    result.pop("ready", None)
    return {
        **result,
        "pending_action": pending,
        "pending_suite_id": str(existing["suite_id"]) if existing else "",
        "suite_check": check,
    }


async def _collect_values(
    *,
    common: dict[str, Any],
    workspace_path: str,
    flow: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    named: list[dict[str, Any]],
    records: list[StoredChunkRecord],
    feature_key: str,
    pending: str,
    pending_suite_id: str,
    rag_top_k: int,
) -> dict[str, Any]:
    """Record the values the user just gave; say what is still missing, if anything."""
    path_nodes = _feature_path(feature_key, nodes) or named
    retrieved, prompt_nodes = _retrieve(
        workspace_path=workspace_path,
        message=common["message"],
        case=common["case"],
        flow=flow,
        named=path_nodes,
        records=records,
        rag_top_k=rag_top_k,
        only_named=bool(path_nodes),
    )
    result = await _complete(
        **common,
        retrieved=retrieved,
        flow_map=prompt_nodes,
        records=records,
        extra={"next_step": "collect", "follow_up": True},
        intent=FEATURE,
        focus_paths=_dependency_paths(prompt_nodes),
        system_prompt=EXPLAIN_PROMPT,
        allow_suite=False,
    )
    ready = result.pop("ready", True) is not False
    emit(
        "info",
        "chat",
        "All values are in; generating now without asking again."
        if ready
        else "Some values are still missing; asking for them.",
    )
    return {
        **result,
        "pending_action": "" if ready else pending,
        "pending_suite_id": pending_suite_id if not ready else "",
    }


async def _generate(
    *,
    common: dict[str, Any],
    workspace_path: str,
    flow: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    named: list[dict[str, Any]],
    records: list[StoredChunkRecord],
    latest_test_suite: dict[str, Any] | None,
    suite_check: dict[str, Any] | None,
    hashes: dict[str, str],
    rag_top_k: int,
) -> dict[str, Any]:
    """The user confirmed (or asked for a change): write the suite or its next version."""
    base_key = str((latest_test_suite or {}).get("module_key") or "")
    base_nodes = [node for node in nodes if node.get("route") == base_key]
    retrieved, prompt_nodes = _retrieve(
        workspace_path=workspace_path,
        message=common["message"],
        case=common["case"],
        flow=flow,
        named=named or base_nodes,
        records=records,
        rag_top_k=rag_top_k,
    )
    result = await _complete(
        **common,
        retrieved=retrieved,
        flow_map=prompt_nodes,
        records=records,
        extra={"suite_check": suite_check} if suite_check else None,
        intent=TEST_GENERATION,
        focus_paths=_dependency_paths(prompt_nodes),
        system_prompt=SYSTEM_PROMPT,
        allow_suite=True,
    )
    _settle_module(
        result, prompt_nodes, latest_test_suite, {str(n.get("route")) for n in nodes}
    )
    result.pop("ready", None)
    suite = result.get("test_suite")
    if isinstance(suite, dict):
        # The saved version matches the code it was written from; a later check
        # with the same fingerprint needs no review.
        merged = {
            "module_key": suite.get("module_key"),
            "cases": [*((latest_test_suite or {}).get("cases") or []), *suite["cases"]],
        }
        result["suite_fingerprint"] = suite_fingerprint(merged, nodes, hashes)
        kinds = [str(case_item.get("kind")) for case_item in suite.get("cases", [])]
        emit(
            "success",
            "suite",
            f"Model returned {len(kinds)} case(s) for {suite.get('feature')} "
            f"({kinds.count('success')} success, {kinds.count('failure')} failure, "
            f"{kinds.count('edge')} edge) on {suite.get('module_key') or 'no screen'}.",
            meta={"module_key": suite.get("module_key"), "cases": len(kinds)},
        )
    return result


def _settle_module(
    result: dict[str, Any],
    prompt_nodes: list[dict[str, Any]],
    base: dict[str, Any] | None,
    known: set[str],
) -> None:
    """A suite always belongs to a real screen: the base suite's, or the closest one."""
    suite = result.get("test_suite")
    if not isinstance(suite, dict):
        return
    if base and base.get("module_key"):
        suite["module_key"] = base["module_key"]
        return
    if suite.get("module_key") in known:
        return
    if prompt_nodes:
        suite["module_key"] = prompt_nodes[0]["module_key"]
        suite["module_name"] = suite.get("module_name") or prompt_nodes[0]["name"]


def _dependency_paths(nodes: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        for path in node.get("dependencies") or []:
            if isinstance(path, str) and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


async def _spec_check(
    *,
    cfg_path: str,
    prd_text: str,
    case: str,
    brief_summary: str,
    flow: dict[str, Any] | None,
    generation_model: str,
    api_key: str,
    rag_top_k: int,
) -> dict[str, Any]:
    try:
        extracted = await extract_prd_requirements(
            prd_text=prd_text,
            generation_model=generation_model,
            api_key=api_key,
        )
    except LlmError as exc:
        emit("error", "prd", f"Could not read requirements from the PRD: {exc}")
        return _plain(str(exc), case, brief_summary, SPEC_CHECK)
    emit(
        "info",
        "prd",
        f'PRD "{extracted["title"]}": {len(extracted["requirements"])} requirements found.',
        meta={"requirements": len(extracted["requirements"])},
    )

    cfg = load_tursor_config(Path(cfg_path))
    records = load_stored_chunks(cfg.embeddings_path)
    nodes = select_nodes(flow, prd_text[:2000], top_k=5)
    # One search per requirement, so a long PRD is not judged on one blended query.
    retrieved: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for requirement in extracted["requirements"]:
        for item in search_chunks(
            records, requirement["text"], top_k=_REQUIREMENT_TOP_K
        ):
            key = (item.get("path"), item.get("start_line"))
            if key not in seen:
                seen.add(key)
                retrieved.append(item)
    retrieved = _with_dependency_chunks(records, nodes, retrieved, rag_top_k)

    try:
        checked = await check_spec(
            prd=extracted,
            case=case,
            flow_map=nodes,
            retrieved_context=retrieved,
            generation_model=generation_model,
            api_key=api_key,
        )
    except LlmError as exc:
        emit("error", "prd", f"Spec check failed: {exc}")
        return _plain(str(exc), case, brief_summary, SPEC_CHECK)

    statuses = [item["status"] for item in checked["requirements"]]
    emit(
        "success" if checked["verdict"] == "aligned" else "warn",
        "prd",
        f"Spec check: {statuses.count('implemented')} implemented, "
        f"{statuses.count('partial')} partial, {statuses.count('missing')} missing "
        f"({len(retrieved)} code chunks checked).",
        meta={
            "verdict": checked["verdict"],
            "gaps": [
                item["id"]
                for item in checked["requirements"]
                if item["status"] != "implemented"
            ],
        },
    )
    return {
        "reply": checked["reply"],
        "case": checked["case"] or case,
        "brief_summary": checked["brief_summary"] or brief_summary,
        "test_suite": None,
        "retrieved_chunk_count": len(retrieved),
        "intent": SPEC_CHECK,
        "focus_paths": _dependency_paths(nodes),
        "prd": {
            "title": extracted["title"],
            "verdict": checked["verdict"],
            "content": prd_text,
            "requirements": checked["requirements"],
        },
    }


async def _complete(
    *,
    generation_model: str,
    api_key: str,
    mode: str,
    message: str,
    case: str,
    brief_summary: str,
    plans: list[dict[str, Any]] | None,
    cdp_runs: list[dict[str, Any]] | None,
    latest_cdp_steps: list[dict[str, Any]] | None,
    latest_test_suite: dict[str, Any] | None,
    retrieved: list[dict[str, Any]],
    flow_map: list[dict[str, Any]],
    feature_names: list[str] | None = None,
    prd: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    intent: str,
    focus_paths: list[str],
    system_prompt: str,
    allow_suite: bool,
    records: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        ai_response = await complete_chat(
            generation_model=generation_model,
            api_key=api_key,
            mode=mode,
            message=message,
            case=case,
            brief_summary=brief_summary,
            plans=plans,
            cdp_runs=cdp_runs,
            latest_cdp_steps=latest_cdp_steps,
            latest_test_suite=latest_test_suite,
            retrieved_context=retrieved,
            flow_map=flow_map,
            feature_names=feature_names,
            prd=prd,
            extra=extra,
            system_prompt=system_prompt,
            intent=intent,
        )
    except LlmError as exc:
        return {
            "reply": str(exc),
            "case": case,
            "brief_summary": brief_summary,
            "test_suite": None,
            "retrieved_chunk_count": len(retrieved),
            "intent": intent,
            "focus_paths": focus_paths,
        }

    if allow_suite:
        sources = [record.text for record in records or []]
        raw_suite = ai_response.get("test_suite")
        grounded = ground_suite(raw_suite, sources)
        dropped = _selector_count(raw_suite) - _selector_count(grounded)
        if dropped > 0:
            emit(
                "warn",
                "suite",
                f"Dropped {dropped} selector(s) that use a class the code does not render.",
                meta={"dropped_selectors": dropped},
            )
        ai_response["test_suite"] = grounded
    else:
        ai_response["test_suite"] = None
    return {
        **ai_response,
        "retrieved_chunk_count": len(retrieved),
        "intent": intent,
        "focus_paths": focus_paths,
    }


def _selector_count(suite: dict[str, Any] | None) -> int:
    if not isinstance(suite, dict):
        return 0
    return sum(
        len(action.get("selectors") or [])
        for case_item in suite.get("cases", [])
        if isinstance(case_item, dict)
        for step in case_item.get("steps", [])
        if isinstance(step, dict)
        for action in step.get("actions", [])
        if isinstance(action, dict)
    )
