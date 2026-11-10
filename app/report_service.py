"""Suite-level test report. Numbers come from the run data; the model only writes prose."""

from __future__ import annotations

import base64
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import MethodReturnValue, TableCellFillMode, XPos, YPos
from fpdf.fonts import FontFace

from app.chat_service import _feature_path
from app.flow_map import flow_nodes, load_flow_map, public_node
from app.llm_service import LlmError, write_report_analysis
from app.log_relay import emit
from app.schemas import ReportMetrics, ReportRequest, ReportResponse
from app.tursor_config import TursorConfigError, load_tursor_config

_MAX_FLOW_NODES = 14
_SECRET_FIELD = re.compile(r"pass|secret|token|otp|\bpin\b|cvv", re.IGNORECASE)
_KIND_LABEL = {
    "success": "Success path",
    "failure": "Failure path",
    "edge": "Edge case",
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "suite"


def _run_time(run: dict[str, Any]) -> str:
    return str(run.get("finished_at") or run.get("started_at") or "")


def _latest_run(case: dict[str, Any]) -> dict[str, Any] | None:
    runs: list[dict[str, Any]] = case.get("runs") or []
    if not runs:
        return None
    return max(runs, key=_run_time)


def compute_metrics(cases: list[dict[str, Any]]) -> ReportMetrics:
    latest = [_latest_run(case) for case in cases]
    executed = [run for run in latest if run]
    passed = sum(1 for run in executed if run.get("status") == "passed")
    return ReportMetrics(
        total=len(cases),
        executed=len(executed),
        passed=passed,
        failed=len(executed) - passed,
        not_run=len(cases) - len(executed),
        runs=sum(len(case.get("runs") or []) for case in cases),
    )


def _status_label(run: dict[str, Any] | None) -> str:
    if not run:
        return "Not run"
    return "Passed" if run.get("status") == "passed" else "Failed"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fmt_time(value: str) -> str:
    parsed = _parse_time(value)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if parsed else ""


def _duration(run: dict[str, Any]) -> str:
    start = _parse_time(str(run.get("started_at") or ""))
    end = _parse_time(str(run.get("finished_at") or ""))
    if not start or not end or end < start:
        return ""
    seconds = (end - start).total_seconds()
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def _traceability(
    prd: dict[str, Any] | None, cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not prd:
        return []
    rows: list[dict[str, Any]] = []
    for requirement in prd.get("requirements") or []:
        covering = [
            case
            for case in cases
            if requirement["id"] in (case.get("requirement_ids") or [])
        ]
        statuses = [_status_label(_latest_run(case)) for case in covering]
        if not covering:
            outcome = "Not covered"
        elif "Failed" in statuses:
            outcome = "Failing"
        elif "Not run" in statuses:
            outcome = "Not fully run"
        else:
            outcome = "Verified"
        rows.append(
            {
                "id": requirement["id"],
                "text": requirement.get("text", ""),
                "implementation": requirement.get("status") or "unknown",
                "gap": requirement.get("gap", ""),
                "cases": [case.get("title", "") for case in covering],
                "outcome": outcome,
            }
        )
    return rows


# --- Steps -----------------------------------------------------------------


def _is_secret(action: dict[str, Any], label: str) -> bool:
    selectors = " ".join(str(item) for item in action.get("selectors") or [])
    return bool(_SECRET_FIELD.search(selectors) or _SECRET_FIELD.search(label))


def _action_text(action: dict[str, Any], label: str) -> str:
    kind = action.get("type")
    selector = next(iter(action.get("selectors") or []), "")
    value = str(action.get("value") or "")
    if kind in {"fill", "select"} and _is_secret(action, label):
        value = "********"
    texts = {
        "navigate": f"Open {action.get('path') or '/'}",
        "click": f"Click {selector}",
        "fill": f'Type "{value}" into {selector}',
        "select": f'Choose "{value}" in {selector}',
        "press": f"Press {action.get('key', '')}"
        + (f" on {selector}" if selector else ""),
        "check": f"Tick {selector}",
        "uncheck": f"Untick {selector}",
        "hover": f"Hover over {selector}",
        "waitForText": f'Wait for the text "{action.get("text", "")}"',
        "waitForPath": "Wait for the address to contain "
        + str(action.get("pathIncludes", "")),
    }
    return texts.get(str(kind), str(kind or "")).strip()


def _steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in case.get("steps") or []:
        if not isinstance(step, dict):
            continue
        label = str(step.get("label") or step.get("id") or "Step")
        actions = [a for a in step.get("actions") or [] if isinstance(a, dict)]
        steps.append(
            {"label": label, "details": [_action_text(a, label) for a in actions]}
        )
    return steps


# --- Model output, validated -------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][
        :limit
    ]


def _by_case(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {
        str(item.get("case_id")): item
        for item in value
        if isinstance(item, dict) and item.get("case_id")
    }


def normalize_flow(raw: object) -> dict[str, Any]:
    """Known nodes, edges between them, and no cycles, so it can be drawn top-down."""
    if not isinstance(raw, dict):
        return {"nodes": [], "edges": []}
    nodes: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        node_id, label = _text(item.get("id")), _text(item.get("label"))
        if node_id and label and node_id not in seen:
            seen.add(node_id)
            nodes.append({"id": node_id, "label": label[:60]})
    nodes = nodes[:_MAX_FLOW_NODES]
    known = {node["id"] for node in nodes}
    edges: list[dict[str, str]] = []
    for item in raw.get("edges") or []:
        if not isinstance(item, dict):
            continue
        source, target = _text(item.get("from")), _text(item.get("to"))
        if source in known and target in known and source != target:
            edges.append(
                {"from": source, "to": target, "label": _text(item.get("label"))[:30]}
            )
    # Drop edges that close a loop, walking from the nodes in the order given.
    out: dict[str, list[dict[str, str]]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        out[edge["from"]].append(edge)
    state: dict[str, int] = {}
    kept: list[dict[str, str]] = []

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for edge in out[node_id]:
            target_state = state.get(edge["to"], 0)
            if target_state == 1:
                continue
            kept.append(edge)
            if target_state == 0:
                visit(edge["to"])
        state[node_id] = 2

    for node in nodes:
        if node["id"] not in state:
            visit(node["id"])
    return {"nodes": nodes, "edges": kept}


def flow_levels(flow: dict[str, Any]) -> list[list[str]]:
    """Rows for a top-down drawing: each node sits one row below its deepest parent."""
    ids = [node["id"] for node in flow["nodes"]]
    parents: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in flow["edges"]:
        parents[edge["to"]].append(edge["from"])
    level: dict[str, int] = {}

    def depth(node_id: str) -> int:
        if node_id not in level:
            level[node_id] = 1 + max((depth(p) for p in parents[node_id]), default=-1)
        return level[node_id]

    rows: list[list[str]] = []
    for node_id in ids:
        row = depth(node_id)
        while len(rows) <= row:
            rows.append([])
        rows[row].append(node_id)
    return rows


def _fallback_flow(
    cases: list[dict[str, Any]], screens: list[dict[str, Any]]
) -> dict[str, Any]:
    """Without the model: the main success case's steps, or the screens on the way."""
    main = next(
        (case for case in cases if case["kind"] == "success" and case["steps"]), None
    )
    labels = (
        [step["label"] for step in main["steps"]]
        if main
        else [str(screen.get("name") or screen.get("module_key")) for screen in screens]
    )[: _MAX_FLOW_NODES - 1]
    nodes = [{"id": f"s{i}", "label": label[:60]} for i, label in enumerate(labels)]
    edges = [
        {"from": f"s{i}", "to": f"s{i + 1}", "label": ""} for i in range(len(nodes) - 1)
    ]
    return {"nodes": nodes, "edges": edges}


# --- Report data ---------------------------------------------------------------


def _feature_screens(workspace_path: str, module_key: str) -> list[dict[str, Any]]:
    """The feature's screen and the screens on the way to it, from the flow map."""
    if not workspace_path or not module_key:
        return []
    try:
        cfg = load_tursor_config(Path(workspace_path))
    except (TursorConfigError, OSError):
        return []
    path = _feature_path(module_key, flow_nodes(load_flow_map(cfg.embeddings_path)))
    screens = []
    for node in path:
        view = public_node(node)
        view.pop("dependencies", None)
        screens.append(view)
    return screens


def build_report_data(
    body: ReportRequest,
    analysis: dict[str, Any],
    screens: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    suite = body.suite.model_dump()
    raw_cases = suite["cases"]
    prd = body.prd.model_dump() if body.prd else None
    metrics = compute_metrics(raw_cases)
    screens = screens or []
    case_notes = _by_case(analysis.get("cases"))
    failure_notes = _by_case(analysis.get("failures"))

    cases: list[dict[str, Any]] = []
    for index, case in enumerate(raw_cases, start=1):
        runs = sorted(case.get("runs") or [], key=_run_time, reverse=True)
        latest = runs[0] if runs else None
        note = case_notes.get(case["case_id"], {})
        failure = failure_notes.get(case["case_id"], {})
        status = _status_label(latest)
        cases.append(
            {
                "index": index,
                "case_id": case["case_id"],
                "title": case.get("title", ""),
                "kind": case.get("kind", ""),
                "status": status,
                "what_it_checks": _text(note.get("what_it_checks"))
                or case.get("explanation", ""),
                "expected": _text(note.get("expected")),
                "requirement_ids": case.get("requirement_ids") or [],
                "steps": _steps(case),
                "runs": [
                    {
                        "run_id": run.get("run_id", ""),
                        "status": _status_label(run),
                        "started_at": _fmt_time(str(run.get("started_at") or "")),
                        "finished_at": _fmt_time(str(run.get("finished_at") or "")),
                        "duration": _duration(run),
                        "run_folder": run.get("run_folder", ""),
                        "failed_step": run.get("failed_step", ""),
                        "status_message": run.get("status_message", ""),
                        "screenshots": run.get("screenshots") or [],
                    }
                    for run in runs
                ],
                "cause": (
                    _text(failure.get("cause"))
                    or (latest or {}).get("status_message", "")
                    if status == "Failed"
                    else ""
                ),
                "next_step": _text(failure.get("next_step"))
                if status == "Failed"
                else "",
            }
        )

    m = metrics
    rate = round(100 * m.passed / m.executed) if m.executed else 0
    overview = _text(analysis.get("overview")) or (
        f"{m.executed} of {m.total} test cases were executed: "
        f"{m.passed} passed and {m.failed} failed ({rate}% pass rate)."
        + (f" {m.not_run} have not been run yet." if m.not_run else "")
    )
    highlights = _strings(analysis.get("highlights"), 6)
    if not highlights:
        failed_titles = [case["title"] for case in cases if case["status"] == "Failed"]
        highlights = [f"{m.passed} of {m.executed} executed cases passed."]
        if failed_titles:
            highlights.append("Failing: " + "; ".join(failed_titles) + ".")
        if m.not_run:
            highlights.append(f"{m.not_run} case(s) have not been run.")

    feature_name = suite.get("module_name") or suite.get("feature") or "Test suite"
    feature_screen = screens[-1] if screens else {}
    feature_overview = _text(analysis.get("feature_overview")) or (
        str(feature_screen.get("summary") or "")
        or f"This suite covers **{feature_name}**."
    )
    how_it_works = _strings(analysis.get("how_it_works"), 10) or [
        str(step) for step in feature_screen.get("how_to_reach") or []
    ]
    flow = normalize_flow(analysis.get("flow"))
    if len(flow["nodes"]) < 2:
        flow = _fallback_flow(cases, screens)

    return {
        "title": feature_name,
        "feature": suite.get("feature", ""),
        "module_key": suite.get("module_key", ""),
        "version": suite.get("version", 1),
        "suite_id": suite["suite_id"],
        "created_at": _fmt_time(str(suite.get("created_at") or "")),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "metrics": metrics.model_dump(),
        "pass_rate": rate,
        "feature_overview": feature_overview,
        "how_it_works": how_it_works,
        "screens": [
            {
                "name": screen.get("name", ""),
                "module_key": screen.get("module_key", ""),
                "requires_auth": screen.get("requires_auth", False),
            }
            for screen in screens
        ],
        "flow": flow,
        "overview": overview,
        "highlights": highlights,
        "recommendations": _strings(analysis.get("recommendations"), 6),
        "cases": cases,
        "failed": [case for case in cases if case["status"] == "Failed"],
        "prd_title": prd.get("title", "") if prd else "",
        "traceability": _traceability(prd, raw_cases),
        "prd_alignment": prd_alignment(prd),
    }


def prd_alignment(prd: dict[str, Any] | None) -> dict[str, Any] | None:
    """The spec check's outcome: its verdict, counts, and every gap it found."""
    if not prd:
        return None
    requirements = prd.get("requirements") or []
    statuses = [str(item.get("status") or "") for item in requirements]
    verdict = prd.get("verdict") or (
        "aligned" if statuses and all(s == "implemented" for s in statuses) else "gaps"
    )
    return {
        "verdict": verdict,
        "implemented": statuses.count("implemented"),
        "partial": statuses.count("partial"),
        "missing": statuses.count("missing"),
        "gaps": [
            {
                "id": item.get("id", ""),
                "text": item.get("text", ""),
                "status": item.get("status", ""),
                "gap": item.get("gap", ""),
            }
            for item in requirements
            if item.get("status") in {"partial", "missing"}
        ],
    }


def _alignment_sentence(alignment: dict[str, Any]) -> str:
    total = alignment["implemented"] + alignment["partial"] + alignment["missing"]
    head = (
        "The implementation is aligned with the PRD."
        if alignment["verdict"] == "aligned"
        else "The implementation has gaps against the PRD."
    )
    return (
        f"{head} Of {total} requirements, {alignment['implemented']} are implemented, "
        f"{alignment['partial']} partially and {alignment['missing']} are missing."
    )


# --- Markdown ------------------------------------------------------------------

_STATUS_ICON = {"Passed": "✅ Passed", "Failed": "❌ Failed", "Not run": "⏸ Not run"}


def _md_cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _mermaid_label(text: str) -> str:
    return text.replace('"', "#quot;").replace("|", "/")


def render_mermaid(flow: dict[str, Any]) -> str:
    ids = {node["id"]: f"n{i}" for i, node in enumerate(flow["nodes"])}
    lines = ["flowchart TD"]
    for node in flow["nodes"]:
        lines.append(f'  {ids[node["id"]]}["{_mermaid_label(node["label"])}"]')
    for edge in flow["edges"]:
        label = f'|"{_mermaid_label(edge["label"])}"|' if edge["label"] else ""
        lines.append(f"  {ids[edge['from']]} -->{label} {ids[edge['to']]}")
    return "\n".join(lines)


def _md_case(case: dict[str, Any]) -> list[str]:
    latest = case["runs"][0] if case["runs"] else None
    kind = _KIND_LABEL.get(case["kind"], case["kind"])
    run_id = f"`{latest['run_id']}`" if latest and latest["run_id"] else "-"
    lines = [
        f"### {case['index']}. {case['title']}",
        "",
        f"**{_STATUS_ICON[case['status']]}** · {kind}",
        "",
        f"- Case ID: `{case['case_id']}`",
        f"- Latest run ID: {run_id}",
    ]
    if case["requirement_ids"]:
        lines.append(f"- Requirements: {', '.join(case['requirement_ids'])}")
    lines.append("")
    if case["what_it_checks"]:
        lines += [f"**What it checks:** {case['what_it_checks']}", ""]
    if case["expected"]:
        lines += [f"**Expected result:** {case['expected']}", ""]
    if case["steps"]:
        lines += ["**Steps**", ""]
        for number, step in enumerate(case["steps"], start=1):
            lines.append(f"{number}. {step['label']}")
            lines += [f"   - `{_md_code(detail)}`" for detail in step["details"]]
        lines.append("")
    if latest:
        lines += [
            "**Latest run**",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Status | {_STATUS_ICON[latest['status']]} |",
            f"| Run ID | `{latest['run_id'] or '-'}` |",
            f"| Started | {latest['started_at'] or '-'} |",
            f"| Finished | {latest['finished_at'] or '-'} |",
            f"| Duration | {latest['duration'] or '-'} |",
            f"| Run folder | `{_md_cell(latest['run_folder'] or '-')}` |",
        ]
        if latest["failed_step"]:
            lines.append(f"| Failing step | {_md_cell(latest['failed_step'])} |")
        lines.append("")
        if latest["status"] == "Failed":
            lines += ["```text", latest["status_message"] or "(no message)", "```", ""]
            if case["cause"]:
                lines += [f"**Likely cause:** {case['cause']}", ""]
            if case["next_step"]:
                lines += [f"**Next step:** {case['next_step']}", ""]
        if latest["screenshots"]:
            links = " · ".join(
                f"[{i}]({url})" for i, url in enumerate(latest["screenshots"], start=1)
            )
            lines += [f"Screenshots: {links}", ""]
    if len(case["runs"]) > 1:
        lines += [
            "<details><summary>Run history</summary>",
            "",
            "| Run ID | Status | Finished | Duration |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{run['run_id'] or '-'}` | {run['status']} "
            f"| {run['finished_at'] or '-'} | {run['duration'] or '-'} |"
            for run in case["runs"]
        ]
        lines += ["", "</details>", ""]
    return lines


def _md_code(text: str) -> str:
    return text.replace("`", "'")


def render_markdown(data: dict[str, Any]) -> str:
    m = data["metrics"]
    lines = [
        f"# Test report: {data['title']} (v{data['version']})",
        "",
        "| | |",
        "| --- | --- |",
        f"| Feature | {_md_cell(data['feature'] or data['title'])} |",
        f"| Module | `{data['module_key'] or '-'}` |",
        f"| Suite ID | `{data['suite_id']}` |",
        f"| Version | {data['version']} |",
        f"| Suite created | {data['created_at'] or '-'} |",
        f"| Report generated | {data['generated_at']} |",
    ]
    if data["prd_title"]:
        lines.append(f"| Linked PRD | {_md_cell(data['prd_title'])} |")
    lines += [
        "",
        "## At a glance",
        "",
        "| Total test cases | Executed | Passed | Failed | Not run | Pass rate |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {m['total']} | {m['executed']} | {m['passed']} | {m['failed']} "
        f"| {m['not_run']} | {data['pass_rate']}% |",
        "",
        "## 1. About the feature",
        "",
        data["feature_overview"],
        "",
    ]
    if data["screens"]:
        lines += ["**Screens involved**", ""]
        lines += [
            f"- {screen['name']} (`{screen['module_key']}`)"
            + (" · needs sign-in" if screen["requires_auth"] else "")
            for screen in data["screens"]
        ]
        lines.append("")
    if data["how_it_works"]:
        lines += ["**How it works**", ""]
        lines += [
            f"{i}. {step}" for i, step in enumerate(data["how_it_works"], start=1)
        ]
        lines.append("")
    if data["flow"]["nodes"]:
        lines += ["**Flow**", "", "```mermaid", render_mermaid(data["flow"]), "```", ""]
    lines += ["## 2. Results", "", data["overview"], ""]
    lines += [f"- {item}" for item in data["highlights"]]
    lines.append("")
    if data["failed"]:
        lines += ["## 3. Failed test cases", ""]
        for case in data["failed"]:
            latest = case["runs"][0]
            lines += [
                f"### {case['title']}",
                "",
                f"- Case ID: `{case['case_id']}`",
                f"- Run ID: `{latest['run_id'] or '-'}`",
                f"- Failing step: {latest['failed_step'] or '-'}",
                f"- Last run: {latest['finished_at'] or '-'}",
                f"- Attempts: {len(case['runs'])}",
            ]
            if case["cause"]:
                lines.append(f"- Likely cause: {case['cause']}")
            if case["next_step"]:
                lines.append(f"- Next step: {case['next_step']}")
            lines.append("")
    section = 4 if data["failed"] else 3
    lines += [f"## {section}. Test cases in detail", ""]
    for case in data["cases"]:
        lines += _md_case(case)
    section += 1
    if data["traceability"]:
        lines += [
            f"## {section}. Requirements traceability ({data['prd_title'] or 'PRD'})",
            "",
        ]
        alignment = data["prd_alignment"]
        if alignment:
            lines += [f"**PRD alignment:** {_alignment_sentence(alignment)}", ""]
            lines += [
                f"- **{gap['id']}** ({gap['status']}): {gap['text']}"
                + (f" Gap: {gap['gap']}" if gap["gap"] else "")
                for gap in alignment["gaps"]
            ]
            if alignment["gaps"]:
                lines.append("")
        lines += [
            "| Requirement | Implementation | Test cases | Result |",
            "| --- | --- | --- | --- |",
        ]
        for row in data["traceability"]:
            cases = ", ".join(row["cases"]) or "-"
            implementation = row["implementation"] + (
                f" ({row['gap']})" if row["gap"] else ""
            )
            lines.append(
                f"| {_md_cell(row['id'] + ': ' + row['text'])} "
                f"| {_md_cell(implementation)} | {_md_cell(cases)} | {row['outcome']} |"
            )
        lines.append("")
        section += 1
    if data["recommendations"]:
        lines += [f"## {section}. Recommendations", ""]
        lines += [f"- {item}" for item in data["recommendations"]]
        lines.append("")
    lines += [
        "## Appendix: all test cases",
        "",
        "| # | Test case | Kind | Status | Runs | Case ID |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in data["cases"]:
        lines.append(
            f"| {case['index']} | {_md_cell(case['title'])} | {case['kind']} "
            f"| {_STATUS_ICON[case['status']]} | {len(case['runs'])} "
            f"| `{case['case_id']}` |"
        )
    return "\n".join(lines) + "\n"


# --- PDF -------------------------------------------------------------------------

INK = (15, 23, 42)
BODY = (51, 65, 85)
MUTED = (100, 116, 139)
ACCENT = (8, 145, 178)
GREEN = (22, 163, 74)
RED = (220, 38, 38)
GRAY = (148, 163, 184)
LIGHT = (241, 245, 249)
BORDER = (203, 213, 225)
WHITE = (255, 255, 255)
_STATUS_COLOR = {"Passed": GREEN, "Failed": RED, "Not run": GRAY}
_KIND_COLOR = {
    "success": (37, 99, 235),
    "failure": (234, 88, 12),
    "edge": (124, 58, 237),
}


def _latin1(text: object) -> str:
    """The built-in PDF fonts cover Latin-1; swap common punctuation, drop the rest."""
    value = str(text)
    for source, target in {
        "\u2026": "...",
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2192": "->",
        "\u00d7": "x",
        "\u2022": "-",
        "\u2265": ">=",
        "\u2264": "<=",
    }.items():
        value = value.replace(source, target)
    return value.encode("latin-1", "replace").decode("latin-1")


def _prose(text: object) -> str:
    """Model prose for fpdf's markdown mode: keep **bold**, defuse its other markers."""
    return _latin1(text).replace("__", "_").replace("--", "-")


class _ReportPdf(FPDF):
    def __init__(self, title: str, suite_id: str) -> None:
        super().__init__(format="A4")
        self.report_title = title
        self.suite_id = suite_id
        self.set_margins(16, 20, 16)
        self.set_auto_page_break(auto=True, margin=18)

    def multi_cell(self, *args: Any, **kwargs: Any) -> Any:
        # fpdf justifies by default, which stretches words; reports read left-aligned.
        kwargs.setdefault("align", "L")
        return super().multi_cell(*args, **kwargs)

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(9)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _latin1(self.report_title))
        self.set_draw_color(*BORDER)
        self.set_line_width(0.2)
        self.line(self.l_margin, 15, self.w - self.r_margin, 15)
        self.set_y(20)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        half = self.epw / 2
        self.cell(half, 5, _latin1(f"Tursor test report  |  Suite {self.suite_id}"))
        self.cell(half, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # Building blocks -----------------------------------------------------------

    def ensure_space(self, height: float) -> None:
        if self.get_y() + height > self.page_break_trigger:
            self.add_page()

    def section(self, text: str) -> None:
        self.ensure_space(30)
        self.ln(4)
        y = self.get_y()
        self.set_fill_color(*ACCENT)
        self.rect(self.l_margin, y + 0.8, 1.6, 6.4, style="F")
        self.set_xy(self.l_margin + 4, y)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*INK)
        self.multi_cell(
            self.epw - 4, 8, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        self.ln(1.5)

    def label(self, text: str) -> None:
        self.ensure_space(14)
        self.ln(1)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*ACCENT)
        self.cell(0, 5, _latin1(text.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def para(self, text: str, size: float = 10, indent: float = 0) -> None:
        self.set_font("Helvetica", "", size)
        self.set_text_color(*BODY)
        for block in [b for b in str(text).split("\n\n") if b.strip()]:
            self.set_x(self.l_margin + indent)
            self.multi_cell(
                self.epw - indent,
                size * 0.52,
                _prose(block.strip()),
                markdown=True,
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
            self.ln(1.5)

    def bullets(
        self, items: list[str], numbered: bool = False, indent: float = 0
    ) -> None:
        for number, item in enumerate(items, start=1):
            self.ensure_space(8)
            y = self.get_y()
            x = self.l_margin + indent
            if numbered:
                self.set_font("Helvetica", "B", 9.5)
                self.set_text_color(*ACCENT)
                self.set_xy(x, y)
                self.cell(6, 5.2, f"{number}.")
            else:
                self.set_fill_color(*ACCENT)
                self.rect(x + 1.2, y + 2, 1.4, 1.4, style="F")
            self.set_xy(x + 6, y)
            self.set_font("Helvetica", "", 9.5)
            self.set_text_color(*BODY)
            self.multi_cell(
                self.epw - indent - 6,
                5.2,
                _prose(item),
                markdown=True,
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
            self.ln(0.8)

    def badge(
        self, x: float, y: float, text: str, color: tuple[int, int, int]
    ) -> float:
        """Draw a pill and return its width."""
        self.set_font("Helvetica", "B", 7.5)
        width = self.get_string_width(text) + 5
        self.set_fill_color(*color)
        self.rect(x, y, width, 5, style="F", round_corners=True, corner_radius=2.5)
        self.set_text_color(*WHITE)
        self.set_xy(x, y)
        self.cell(width, 5, text, align="C")
        return width

    def key_values(
        self, rows: list[tuple[str, str]], mono: set[str] | None = None
    ) -> None:
        mono = mono or set()
        for key, value in rows:
            self.ensure_space(6)
            y = self.get_y()
            self.set_font("Helvetica", "B", 8.5)
            self.set_text_color(*MUTED)
            self.set_xy(self.l_margin + 2, y)
            self.cell(32, 5, _latin1(key))
            self.set_xy(self.l_margin + 34, y)
            if key in mono:
                self.set_font("Courier", "", 8.5)
            else:
                self.set_font("Helvetica", "", 9)
            self.set_text_color(*INK)
            self.multi_cell(
                self.epw - 34,
                5,
                _latin1(value or "-"),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )

    def mono_box(self, text: str, color: tuple[int, int, int]) -> None:
        self.set_font("Courier", "", 8)
        lines = self.multi_cell(
            self.epw - 8, 4, _latin1(text), dry_run=True, output=MethodReturnValue.LINES
        )
        height = len(lines) * 4 + 4
        self.ensure_space(height + 2)
        y = self.get_y()
        self.set_fill_color(254, 242, 242)
        self.rect(self.l_margin, y, self.epw, height, style="F")
        self.set_fill_color(*color)
        self.rect(self.l_margin, y, 1.2, height, style="F")
        self.set_xy(self.l_margin + 4, y + 2)
        self.set_text_color(*INK)
        self.multi_cell(
            self.epw - 8, 4, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        self.set_y(y + height + 2)

    def link_row(self, label: str, urls: list[str]) -> None:
        self.ensure_space(6)
        self.set_font("Helvetica", "B", 8.5)
        self.set_text_color(*MUTED)
        self.set_x(self.l_margin + 2)
        self.cell(32, 5, _latin1(label))
        self.set_font("Helvetica", "U", 8.5)
        self.set_text_color(*ACCENT)
        for number, url in enumerate(urls, start=1):
            text = str(number)
            width = self.get_string_width(text) + 3
            if self.get_x() + width > self.w - self.r_margin:
                self.ln(5)
                self.set_x(self.l_margin + 34)
            self.cell(width, 5, text, link=url)
        self.ln(6)

    def grid(
        self,
        rows: list[list[str]],
        widths: list[float],
        colors: dict[tuple[int, int], tuple[int, int, int]] | None = None,
        mono_cols: set[int] | None = None,
    ) -> None:
        colors = colors or {}
        mono_cols = mono_cols or set()
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*INK)
        self.set_draw_color(*BORDER)
        # Unfilled rows use the current fill colour.
        self.set_fill_color(*WHITE)
        with self.table(
            col_widths=widths,
            text_align="LEFT",
            line_height=4.6,
            padding=1.6,
            borders_layout="HORIZONTAL_LINES",
            headings_style=FontFace(emphasis="BOLD", color=WHITE, fill_color=INK),
            cell_fill_color=LIGHT,
            cell_fill_mode=TableCellFillMode.ROWS,
        ) as table:
            for r, row in enumerate(rows):
                cells = table.row()
                for c, value in enumerate(row):
                    style = None
                    if r and (r, c) in colors:
                        style = FontFace(emphasis="BOLD", color=colors[(r, c)])
                    elif r and c in mono_cols:
                        style = FontFace(family="Courier", size_pt=7)
                    cells.cell(_latin1(value), style=style)
        self.ln(2)


def _cover(pdf: _ReportPdf, data: dict[str, Any]) -> None:
    pdf.set_fill_color(*INK)
    pdf.rect(0, 0, pdf.w, 46, style="F")
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 46, pdf.w, 1.2, style="F")
    pdf.set_xy(pdf.l_margin, 11)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(103, 232, 249)
    pdf.cell(0, 5, "TURSOR  |  TEST REPORT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*WHITE)
    pdf.multi_cell(
        pdf.epw,
        9,
        _latin1(f"{data['title']} (v{data['version']})"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(203, 213, 225)
    pdf.cell(
        0,
        5,
        _latin1(
            f"Module {data['module_key'] or '-'}   |   Generated {data['generated_at']}"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_y(54)
    rows = [
        ("Feature", data["feature"] or data["title"]),
        ("Module", data["module_key"] or "-"),
        ("Suite ID", data["suite_id"]),
        ("Version", str(data["version"])),
        ("Suite created", data["created_at"] or "-"),
        ("Report generated", data["generated_at"]),
    ]
    if data["prd_title"]:
        rows.append(("Linked PRD", data["prd_title"]))
    pdf.key_values(rows, mono={"Suite ID", "Module"})
    pdf.ln(4)
    _kpis(pdf, data)


def _kpis(pdf: _ReportPdf, data: dict[str, Any]) -> None:
    m = data["metrics"]
    tiles = [
        ("Total cases", m["total"], INK),
        ("Executed", m["executed"], ACCENT),
        ("Passed", m["passed"], GREEN),
        ("Failed", m["failed"], RED),
        ("Not run", m["not_run"], GRAY),
    ]
    gap = 3.0
    width = (pdf.epw - gap * (len(tiles) - 1)) / len(tiles)
    y = pdf.get_y()
    for i, (label, value, color) in enumerate(tiles):
        x = pdf.l_margin + i * (width + gap)
        pdf.set_fill_color(*LIGHT)
        pdf.rect(x, y, width, 21, style="F", round_corners=True, corner_radius=2)
        pdf.set_fill_color(*color)
        pdf.rect(x, y, width, 1.4, style="F")
        pdf.set_xy(x, y + 4)
        pdf.set_font("Helvetica", "B", 17)
        pdf.set_text_color(*color)
        pdf.cell(width, 8, str(value), align="C")
        pdf.set_xy(x, y + 13)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(width, 5, label, align="C")
    y += 26
    # Pass-rate bar across all cases: passed, failed, not run.
    total = max(m["total"], 1)
    x = pdf.l_margin
    for count, color in (
        (m["passed"], GREEN),
        (m["failed"], RED),
        (m["not_run"], GRAY),
    ):
        if count:
            width = pdf.epw * count / total
            pdf.set_fill_color(*color)
            pdf.rect(x, y, width, 3, style="F")
            x += width
    pdf.set_xy(pdf.l_margin, y + 4.5)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(
        0,
        5,
        f"Pass rate {data['pass_rate']}% of executed cases  "
        f"({m['passed']} passed, {m['failed']} failed, {m['not_run']} not run; "
        f"{m['runs']} runs in total)",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )


def _arrow(pdf: _ReportPdf, x1: float, y1: float, x2: float, y2: float) -> None:
    pdf.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    size, spread = 2.4, 0.45
    pdf.polygon(
        [
            (x2, y2),
            (
                x2 - size * math.cos(angle - spread),
                y2 - size * math.sin(angle - spread),
            ),
            (
                x2 - size * math.cos(angle + spread),
                y2 - size * math.sin(angle + spread),
            ),
        ],
        style="F",
    )


def draw_flow(pdf: _ReportPdf, flow: dict[str, Any], title: str) -> None:
    rows = flow_levels(flow)
    labels = {node["id"]: node["label"] for node in flow["nodes"]}
    has_children = {edge["from"] for edge in flow["edges"]}
    gap, vgap, line_h = 6.0, 14.0, 3.8
    pdf.set_font("Helvetica", "B", 8)
    boxes: dict[str, tuple[float, float, float, float]] = {}
    layout: list[tuple[list[str], float, list[float], float]] = []
    for row in rows:
        width = min(54.0, (pdf.epw - gap * (len(row) - 1)) / len(row))
        heights = [
            len(
                pdf.multi_cell(
                    width - 4,
                    line_h,
                    _latin1(labels[node_id]),
                    dry_run=True,
                    output=MethodReturnValue.LINES,
                )
            )
            * line_h
            + 4
            for node_id in row
        ]
        layout.append((row, width, heights, max(heights)))
    total = sum(item[3] for item in layout) + vgap * (len(layout) - 1)
    # Keep the title on the same page as the diagram.
    pdf.ensure_space(total + 20)
    pdf.label(title)
    y = pdf.get_y() + 2
    for row, width, heights, row_h in layout:
        span = width * len(row) + gap * (len(row) - 1)
        x = pdf.l_margin + (pdf.epw - span) / 2
        for node_id, height in zip(row, heights, strict=True):
            boxes[node_id] = (x, y + (row_h - height) / 2, width, height)
            x += width + gap
        y += row_h + vgap
    # Edges under the boxes, labels on top of the lines.
    pdf.set_draw_color(*MUTED)
    pdf.set_fill_color(*MUTED)
    pdf.set_line_width(0.3)
    # Elbow connectors: down from the parent, across just above the child, then down.
    for edge in flow["edges"]:
        sx, sy, sw, sh = boxes[edge["from"]]
        tx, ty, tw, _ = boxes[edge["to"]]
        x1, x2 = sx + sw / 2, tx + tw / 2
        if abs(x1 - x2) < 0.5:
            _arrow(pdf, x1, sy + sh, x2, ty)
            continue
        bend = ty - vgap / 2
        pdf.line(x1, sy + sh, x1, bend)
        pdf.line(x1, bend, x2, bend)
        _arrow(pdf, x2, bend, x2, ty)
    for index, (node_id, (x, y0, w, h)) in enumerate(boxes.items()):
        start = index == 0
        leaf = node_id not in has_children
        fill = ACCENT if start else (LIGHT if leaf else WHITE)
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*(ACCENT if not leaf or start else BORDER))
        pdf.set_line_width(0.4)
        pdf.rect(x, y0, w, h, style="DF", round_corners=True, corner_radius=2)
        pdf.set_xy(x + 2, y0 + 2)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*(WHITE if start else INK))
        pdf.multi_cell(w - 4, line_h, _latin1(labels[node_id]), align="C")
    pdf.set_font("Helvetica", "I", 7)
    for edge in flow["edges"]:
        if not edge["label"]:
            continue
        sx, sy, sw, sh = boxes[edge["from"]]
        tx, ty, tw, _ = boxes[edge["to"]]
        x1, x2 = sx + sw / 2, tx + tw / 2
        # On the child's own drop, so labels of a fan-out never share a line.
        mx = x2
        my = (sy + sh + ty) / 2 if abs(x1 - x2) < 0.5 else ty - vgap / 4 - 0.6
        text = _latin1(edge["label"])
        width = pdf.get_string_width(text) + 2.4
        pdf.set_fill_color(*WHITE)
        pdf.rect(mx - width / 2, my - 2, width, 4, style="F")
        pdf.set_xy(mx - width / 2, my - 2)
        pdf.set_text_color(*MUTED)
        pdf.cell(width, 4, text, align="C")
    pdf.set_line_width(0.2)
    pdf.set_y(y + 2)


def _case_card(pdf: _ReportPdf, case: dict[str, Any]) -> None:
    pdf.ensure_space(48)
    pdf.ln(2)
    title = _latin1(f"{case['index']}. {case['title']}")
    pdf.set_font("Helvetica", "B", 11)
    title_w = pdf.epw - 58
    lines = pdf.multi_cell(
        title_w, 5.5, title, dry_run=True, output=MethodReturnValue.LINES
    )
    height = len(lines) * 5.5 + 5
    y = pdf.get_y()
    pdf.set_fill_color(*LIGHT)
    pdf.rect(
        pdf.l_margin, y, pdf.epw, height, style="F", round_corners=True, corner_radius=2
    )
    pdf.set_fill_color(*_STATUS_COLOR[case["status"]])
    pdf.rect(pdf.l_margin, y, 1.6, height, style="F")
    pdf.set_xy(pdf.l_margin + 5, y + 2.5)
    pdf.set_text_color(*INK)
    pdf.multi_cell(title_w, 5.5, title)
    # Pills on the right: kind, then status at the edge.
    status = case["status"]
    kind = _KIND_LABEL.get(case["kind"], case["kind"] or "Case")
    pdf.set_font("Helvetica", "B", 7.5)
    right = pdf.w - pdf.r_margin - 3
    status_w = pdf.get_string_width(status) + 5
    kind_w = pdf.get_string_width(kind) + 5
    pdf.badge(right - status_w, y + 2.8, status, _STATUS_COLOR[status])
    pdf.badge(
        right - status_w - 2 - kind_w,
        y + 2.8,
        kind,
        _KIND_COLOR.get(case["kind"], MUTED),
    )
    pdf.set_y(y + height + 2)

    latest = case["runs"][0] if case["runs"] else None
    ids = [
        ("Case ID", case["case_id"]),
        ("Latest run ID", latest["run_id"] if latest else "Not run yet"),
    ]
    if case["requirement_ids"]:
        ids.append(("Requirements", ", ".join(case["requirement_ids"])))
    pdf.key_values(ids, mono={"Case ID", "Latest run ID"})

    if case["what_it_checks"]:
        pdf.label("What it checks")
        pdf.para(case["what_it_checks"], size=9.5)
    if case["expected"]:
        pdf.label("Expected result")
        pdf.para(case["expected"], size=9.5)
    if case["steps"]:
        pdf.label("Steps")
        for number, step in enumerate(case["steps"], start=1):
            pdf.ensure_space(10)
            row_y = pdf.get_y()
            pdf.set_xy(pdf.l_margin, row_y)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*ACCENT)
            pdf.cell(7, 5, f"{number}.")
            pdf.set_xy(pdf.l_margin + 7, row_y)
            pdf.set_text_color(*INK)
            pdf.multi_cell(
                pdf.epw - 7,
                5,
                _latin1(step["label"]),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
            pdf.set_font("Courier", "", 7.5)
            pdf.set_text_color(*MUTED)
            for detail in step["details"]:
                pdf.set_x(pdf.l_margin + 7)
                pdf.multi_cell(
                    pdf.epw - 7,
                    3.8,
                    _latin1(detail),
                    new_x=XPos.LMARGIN,
                    new_y=YPos.NEXT,
                )
            pdf.ln(0.8)
    if latest:
        pdf.label("Latest run")
        rows = [
            ("Status", latest["status"]),
            ("Run ID", latest["run_id"]),
            ("Started", latest["started_at"]),
            ("Finished", latest["finished_at"]),
            ("Duration", latest["duration"]),
            ("Run folder", latest["run_folder"]),
        ]
        if latest["failed_step"]:
            rows.append(("Failing step", latest["failed_step"]))
        pdf.key_values(rows, mono={"Run ID", "Run folder"})
        if latest["screenshots"]:
            pdf.link_row("Screenshots", latest["screenshots"])
        if latest["status"] == "Failed":
            pdf.ln(1)
            pdf.mono_box(latest["status_message"] or "(no message)", RED)
            if case["cause"]:
                pdf.label("Likely cause")
                pdf.para(case["cause"], size=9.5)
            if case["next_step"]:
                pdf.label("Next step")
                pdf.para(case["next_step"], size=9.5)
    if len(case["runs"]) > 1:
        pdf.label(f"Run history ({len(case['runs'])} runs)")
        rows_ = [["Run ID", "Status", "Finished", "Duration"]]
        colors: dict[tuple[int, int], tuple[int, int, int]] = {}
        for r, run in enumerate(case["runs"], start=1):
            rows_.append(
                [
                    run["run_id"] or "-",
                    run["status"],
                    run["finished_at"] or "-",
                    run["duration"] or "-",
                ]
            )
            colors[(r, 1)] = _STATUS_COLOR[run["status"]]
        pdf.grid(rows_, [2.6, 0.9, 1.8, 0.8], colors=colors, mono_cols={0})
    pdf.ln(3)
    pdf.set_draw_color(*BORDER)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)


def render_pdf(data: dict[str, Any]) -> bytes:
    pdf = _ReportPdf(f"{data['title']} (v{data['version']})", data["suite_id"])
    pdf.add_page()
    _cover(pdf, data)

    pdf.section("1. About the feature")
    pdf.para(data["feature_overview"])
    if data["screens"]:
        pdf.label("Screens involved")
        pdf.bullets(
            [
                f"**{screen['name']}** ({screen['module_key']})"
                + (", needs sign-in" if screen["requires_auth"] else "")
                for screen in data["screens"]
            ]
        )
    if data["how_it_works"]:
        pdf.label("How it works")
        pdf.bullets(data["how_it_works"], numbered=True)
    if len(data["flow"]["nodes"]) > 1:
        draw_flow(pdf, data["flow"], "Flow")

    pdf.section("2. Results")
    pdf.para(data["overview"])
    if data["highlights"]:
        pdf.label("Key findings")
        pdf.bullets(data["highlights"])

    section = 3
    if data["failed"]:
        pdf.section(f"{section}. Failed test cases")
        rows = [["Test case", "Failing step", "Likely cause"]]
        for case in data["failed"]:
            rows.append(
                [
                    f"{case['title']}\n{case['case_id']}",
                    case["runs"][0]["failed_step"] or "-",
                    case["cause"] or "-",
                ]
            )
        pdf.grid(rows, [2.2, 1.2, 2.2])
        section += 1

    pdf.section(f"{section}. Test cases in detail")
    for case in data["cases"]:
        _case_card(pdf, case)
    section += 1

    if data["traceability"]:
        pdf.section(
            f"{section}. Requirements traceability ({data['prd_title'] or 'PRD'})"
        )
        alignment = data["prd_alignment"]
        if alignment:
            pdf.label("PRD alignment")
            pdf.para(_alignment_sentence(alignment))
            if alignment["gaps"]:
                pdf.bullets(
                    [
                        f"**{gap['id']}** ({gap['status']}): {gap['text']}"
                        + (f" Gap: {gap['gap']}" if gap["gap"] else "")
                        for gap in alignment["gaps"]
                    ]
                )
            pdf.label("Coverage by test cases")
        rows = [["Requirement", "Implementation", "Test cases", "Result"]]
        colors: dict[tuple[int, int], tuple[int, int, int]] = {}
        outcome_color = {
            "Verified": GREEN,
            "Failing": RED,
            "Not covered": GRAY,
            "Not fully run": GRAY,
        }
        for r, row in enumerate(data["traceability"], start=1):
            implementation = row["implementation"] + (
                f" ({row['gap']})" if row["gap"] else ""
            )
            rows.append(
                [
                    f"{row['id']}: {row['text']}",
                    implementation,
                    ", ".join(row["cases"]) or "-",
                    row["outcome"],
                ]
            )
            colors[(r, 3)] = outcome_color.get(row["outcome"], INK)
        pdf.grid(rows, [2.4, 1.6, 1.8, 1], colors=colors)
        section += 1

    if data["recommendations"]:
        pdf.section(f"{section}. Recommendations")
        pdf.bullets(data["recommendations"], numbered=True)

    pdf.section("Appendix: all test cases")
    rows = [["#", "Test case", "Kind", "Status", "Runs", "Case ID"]]
    colors = {}
    for r, case in enumerate(data["cases"], start=1):
        rows.append(
            [
                str(case["index"]),
                case["title"],
                case["kind"],
                case["status"],
                str(len(case["runs"])),
                case["case_id"],
            ]
        )
        colors[(r, 3)] = _STATUS_COLOR[case["status"]]
    pdf.grid(rows, [0.35, 2.8, 0.75, 0.8, 0.5, 2.9], colors=colors, mono_cols={5})
    return bytes(pdf.output())


# --- Entry point -------------------------------------------------------------------


async def generate_report(body: ReportRequest) -> ReportResponse:
    cases = [case.model_dump() for case in body.suite.cases]
    metrics = compute_metrics(cases)
    screens = _feature_screens(body.workspace_path, body.suite.module_key)
    failed_context = []
    for case in cases:
        run = _latest_run(case)
        if run and run.get("status") == "failure":
            failed_context.append(
                {
                    "case_id": case["case_id"],
                    "title": case.get("title", ""),
                    "status_message": run.get("status_message", ""),
                    "failed_step": run.get("failed_step", ""),
                }
            )
    context = {
        "feature": {
            "name": body.suite.module_name or body.suite.feature,
            "description": body.suite.feature,
            "module_key": body.suite.module_key,
            "screens": screens,
        },
        "version": body.suite.version,
        "metrics": metrics.model_dump(),
        "cases": [
            {
                "case_id": case["case_id"],
                "kind": case.get("kind", ""),
                "title": case.get("title", ""),
                "explanation": case.get("explanation", ""),
                "steps": [step["label"] for step in _steps(case)],
                "latest": _status_label(_latest_run(case)),
            }
            for case in cases
        ],
        "failed_cases": failed_context,
        "prd": body.prd.model_dump() if body.prd else None,
    }
    try:
        analysis = await write_report_analysis(
            context=context,
            generation_model=body.generation_model,
            api_key=body.api_key,
        )
    except LlmError as exc:
        # The report is still useful without prose; numbers and failures stand alone.
        emit("warn", "report", f"Report prose unavailable, using run data only: {exc}")
        analysis = {}

    data = build_report_data(body, analysis, screens)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    base = f"{_slug(data['title'])}_v{data['version']}_report_{stamp}"
    if body.format == "pdf":
        return ReportResponse(
            format="pdf",
            filename=f"{base}.pdf",
            content_type="application/pdf",
            encoding="base64",
            content=base64.b64encode(render_pdf(data)).decode("ascii"),
            metrics=metrics,
        )
    return ReportResponse(
        format="md",
        filename=f"{base}.md",
        content_type="text/markdown; charset=utf-8",
        encoding="utf-8",
        content=render_markdown(data),
        metrics=metrics,
    )
