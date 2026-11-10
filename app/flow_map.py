"""A flow map is a tree of the app's screens, from the first page down.

Routes and links are read from the code, so every screen has a stable key (its route).
The model only adds what the code alone cannot say well: a screen name, its fields,
the labels on the controls that lead elsewhere, and error text. It is called for small
batches of screens, a few at a time in parallel, so a large app is never cut off by a
single prompt limit. A batch that fails keeps the code-only details for its screens.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from app.embedder import embed_texts
from app.log_relay import Timer, emit
from app.rag_service import _cosine_similarity
from app.storage import StoredChunkRecord, load_manifest, write_atomic
from app.tursor_config import TursorWorkspaceConfig

MAP_FILE = "flow-map.json"
LEGACY_MAP_FILE = "feature-map.json"
ROOT = "/"
# About two or three pages per call: a reasoning model answers that well inside the timeout.
_BATCH_LIMIT = 24_000
_FILE_LIMIT = 16_000
_PARALLEL_CALLS = 4
_CALL_TIMEOUT = httpx.Timeout(240.0, connect=15.0)
_CALL_ATTEMPTS = 2


_SCRIPT_SUFFIXES = (".tsx", ".ts", ".jsx", ".js")

_ROUTE_OPEN = re.compile(r"<Route\b|</Route\s*>")
_ATTR_PATH = re.compile(
    r"\bpath\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|\{\s*[\"']([^\"']*)[\"']\s*\})"
)
_ATTR_ELEMENT = re.compile(r"\belement\s*=\s*\{\s*<\s*([A-Za-z_][\w.]*)")
_ATTR_INDEX = re.compile(r"\bindex\b")
_OBJECT_ROUTE = re.compile(
    r"\bpath\s*:\s*[\"']([^\"']+)[\"'][^{}]*?\belement\s*:\s*<\s*([A-Za-z_][\w.]*)",
    re.DOTALL,
)
_IMPORT = re.compile(
    r"import\s+(?:\{([^}]*)\}|([A-Za-z_]\w*))(?:\s*,\s*\{([^}]*)\})?\s+from\s+[\"'](\.{1,2}/[^\"']+)[\"']",
)
_LINK = re.compile(
    r"<(?:Link|NavLink|a)\b[^>]*?\b(?:to|href)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|\{\s*`([^`]+)`\s*\}|\{\s*[\"']([^\"']+)[\"']\s*\})[^>]*>(.*?)</(?:Link|NavLink|a)>",
    re.DOTALL,
)
_NAVIGATE = re.compile(r"\bnavigate\(\s*(?:\"([^\"]+)\"|'([^']+)'|`([^`]+)`)")
_ROUTE_LITERAL = re.compile(r"[\"'`](/[\w\-/:.${}]*)[\"'`]")
_TAGS = re.compile(r"<[^>]+>|\{[^}]*\}")
_INTERP = re.compile(r"\$\{[^}]*\}")

_SCREEN_PROMPT = """You are reading screens of a frontend app. For each screen file, describe what a tester sees.

Respond with one JSON object and no markdown fences:
{
  "screens": [
    {
      "route": "/login",
      "name": "short screen name, such as Sign in",
      "summary": "one sentence on what the screen is for",
      "fields": ["each input a person fills, by its visible label"],
      "actions": [{"label": "visible text of the button or link", "to": "/route it opens, or null"}],
      "errors": ["error text that stays on the page"]
    }
  ]
}

Use only the routes in known_routes for "to". Copy labels exactly from the code. A button whose label changes by step lists the label for each step.
"""


def map_path(embeddings_dir: Path) -> Path:
    return embeddings_dir / MAP_FILE


def load_flow_map(embeddings_dir: Path) -> dict[str, Any] | None:
    path = map_path(embeddings_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), dict):
        return None
    return raw


def flow_nodes(flow: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not flow:
        return []
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict):
        return []
    return [node for node in nodes.values() if isinstance(node, dict)]


def _grouped_text(records: list[StoredChunkRecord]) -> dict[str, str]:
    grouped: dict[str, list[StoredChunkRecord]] = {}
    for record in records:
        grouped.setdefault(record.path, []).append(record)
    texts: dict[str, str] = {}
    for path, items in grouped.items():
        items.sort(key=lambda item: item.chunk_index)
        # Chunks overlap; stitch by line ranges so a file reads once.
        lines: dict[int, str] = {}
        for item in items:
            for offset, line in enumerate(item.text.splitlines()):
                lines.setdefault(item.start_line + offset, line)
        texts[path] = "\n".join(lines[number] for number in sorted(lines))
    return texts


def _resolve_import(base: str, spec: str, known: set[str]) -> str | None:
    folder = Path(base).parent
    joined = os.path.normpath(str(folder / spec)).replace("\\", "/")
    candidates = [joined, *(joined + suffix for suffix in _SCRIPT_SUFFIXES)]
    candidates += [f"{joined}/index{suffix}" for suffix in _SCRIPT_SUFFIXES]
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


def _local_imports(path: str, text: str, known: set[str]) -> dict[str, str]:
    """Map each imported name to the workspace file it comes from."""
    names: dict[str, str] = {}
    for match in _IMPORT.finditer(text):
        braced, default, braced_after, spec = match.groups()
        target = _resolve_import(path, spec, known)
        if not target:
            continue
        if default:
            names[default] = target
        for group in (braced, braced_after):
            for part in (group or "").split(","):
                name = part.split(" as ")[-1].strip()
                if name:
                    names[name] = target
    return names


def _join_route(parent: str, child: str) -> str:
    if child.startswith("/"):
        return child
    base = parent.rstrip("/")
    return f"{base}/{child}" if base else f"/{child}"


def _route_tags(text: str) -> list[tuple[str, str, bool]]:
    """Yield ("open", attrs, self_closing) or ("close", "", False) for each Route tag.

    Attributes hold JSX such as element={<Guard />}, so the tag ends at the first
    ">" outside braces, not the first ">" in the text.
    """
    tags: list[tuple[str, str, bool]] = []
    for match in _ROUTE_OPEN.finditer(text):
        if match.group(0).startswith("</"):
            tags.append(("close", "", False))
            continue
        depth = 0
        index = match.end()
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == ">" and depth == 0:
                break
            index += 1
        attrs = text[match.end() : index]
        self_closing = attrs.rstrip().endswith("/")
        tags.append(("open", attrs.rstrip().rstrip("/"), self_closing))
    return tags


def extract_routes(texts: dict[str, str]) -> list[dict[str, Any]]:
    """Read react-router routes (JSX or object form) with their guard layouts."""
    known = set(texts)
    routes: dict[str, dict[str, Any]] = {}
    for path, text in texts.items():
        if not path.endswith(_SCRIPT_SUFFIXES):
            continue
        imports = _local_imports(path, text, known)
        stack: list[dict[str, str | None]] = []
        for kind, attrs, self_closing in _route_tags(text):
            if kind == "close":
                if stack:
                    stack.pop()
                continue
            path_match = _ATTR_PATH.search(attrs)
            element_match = _ATTR_ELEMENT.search(attrs)
            element = element_match.group(1) if element_match else None
            parent_path = next(
                (frame["path"] for frame in reversed(stack) if frame["path"]), ""
            )
            route_path: str | None = None
            if path_match:
                raw = next(group for group in path_match.groups() if group is not None)
                route_path = _join_route(parent_path or "", raw)
            elif _ATTR_INDEX.search(attrs):
                route_path = parent_path or ROOT
            guards = [
                frame["element"]
                for frame in stack
                if frame["element"] and not frame["path"]
            ]
            if route_path and element:
                routes.setdefault(
                    route_path,
                    {
                        "route": route_path,
                        "component": element,
                        "file": imports.get(element, path if element in text else ""),
                        "guards": [guard for guard in guards if guard],
                    },
                )
            if not self_closing:
                stack.append({"path": route_path, "element": element})
        for match in _OBJECT_ROUTE.finditer(text):
            route_path, element = match.group(1), match.group(2)
            routes.setdefault(
                _join_route("", route_path),
                {
                    "route": _join_route("", route_path),
                    "component": element,
                    "file": imports.get(element, ""),
                    "guards": [],
                },
            )
    return sorted(
        routes.values(), key=lambda item: (item["route"] != ROOT, item["route"])
    )


def _route_pattern(route: str) -> re.Pattern[str]:
    parts = []
    for segment in route.strip("/").split("/"):
        if segment.startswith(":") or segment == "*":
            parts.append("[^/]+")
        else:
            parts.append(re.escape(segment))
    return (
        re.compile("^/" + "/".join(parts) + "/?$")
        if parts != [""]
        else re.compile("^/?$")
    )


def _match_route(
    target: str, patterns: list[tuple[str, re.Pattern[str]]]
) -> str | None:
    clean = _INTERP.sub("x", target.split("?")[0].split("#")[0])
    if not clean.startswith("/"):
        return None
    for route, pattern in patterns:
        if pattern.match(clean):
            return route
    return None


def _label(raw: str) -> str:
    text = " ".join(_TAGS.sub(" ", raw).split())
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def extract_links(
    text: str, patterns: list[tuple[str, re.Pattern[str]]]
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _LINK.finditer(text):
        target = next(group for group in match.groups()[:4] if group is not None)
        route = _match_route(target, patterns)
        if not route:
            continue
        key = (route, _label(match.group(5)))
        if key not in seen:
            seen.add(key)
            links.append({"label": key[1], "to": route})
    for match in _NAVIGATE.finditer(text):
        target = next(group for group in match.groups() if group is not None)
        route = _match_route(target, patterns)
        if (
            route
            and (route, "") not in seen
            and not any(item["to"] == route for item in links)
        ):
            seen.add((route, ""))
            links.append({"label": "", "to": route})
    # Navigation often lives in data, such as [{ to: '/studio/upload', label: 'Upload' }].
    for match in _ROUTE_LITERAL.finditer(text):
        route = _match_route(match.group(1), patterns)
        if route and not any(item["to"] == route for item in links):
            links.append({"label": "", "to": route})
    return links


def _screen_files(route: dict[str, Any], texts: dict[str, str]) -> list[str]:
    """The page file plus the local components it imports (one level)."""
    page = route.get("file")
    if not page or page not in texts:
        return []
    files = [page]
    for target in _local_imports(page, texts[page], set(texts)).values():
        if target not in files and target.endswith(_SCRIPT_SUFFIXES):
            files.append(target)
    return files


def _default_name(component: str, route: str) -> str:
    base = re.sub(r"(Page|Screen|View)$", "", component) or route
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", base).strip()
    return words or route


def _batches(
    routes: list[dict[str, Any]], texts: dict[str, str]
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for route in routes:
        page = route.get("file")
        size = min(len(texts.get(page, "")), _FILE_LIMIT) if page else 0
        if current and used + size > _BATCH_LIMIT:
            batches.append(current)
            current, used = [], 0
        current.append(route)
        used += size
    if current:
        batches.append(current)
    return batches


def _describe_batch(
    cfg: TursorWorkspaceConfig,
    batch: list[dict[str, Any]],
    texts: dict[str, str],
    known_routes: list[str],
    client: httpx.Client,
    openai_base_url: str,
) -> dict[str, dict[str, Any]]:
    assert cfg.ai is not None
    parts = [f"known_routes: {json.dumps(known_routes)}"]
    for route in batch:
        page = route.get("file")
        if not page or page not in texts:
            continue
        body = texts[page][:_FILE_LIMIT]
        parts.append(f"--- route {route['route']} · {page} ---\n{body}")
    res = client.post(
        f"{openai_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg.ai.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.ai.generation_model,
            "messages": [
                {"role": "system", "content": _SCREEN_PROMPT},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            "temperature": 1,
            "response_format": {"type": "json_object"},
        },
    )
    res.raise_for_status()
    content = res.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    screens = data.get("screens") if isinstance(data, dict) else None
    described: dict[str, dict[str, Any]] = {}
    for item in screens if isinstance(screens, list) else []:
        if isinstance(item, dict) and isinstance(item.get("route"), str):
            described[item["route"]] = item
    return described


def _describe_batch_safely(
    cfg: TursorWorkspaceConfig,
    batch: list[dict[str, Any]],
    texts: dict[str, str],
    known_routes: list[str],
    client: httpx.Client,
    openai_base_url: str,
) -> dict[str, dict[str, Any]] | None:
    """Describe one batch, retrying once; None when the model never answered usefully."""
    routes = ", ".join(route["route"] for route in batch)
    for attempt in range(1, _CALL_ATTEMPTS + 1):
        timer = Timer()
        try:
            described = _describe_batch(
                cfg, batch, texts, known_routes, client, openai_base_url
            )
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
            reason = (
                "timed out" if isinstance(exc, httpx.TimeoutException) else str(exc)
            )
            emit(
                "warn",
                "flowmap",
                f"Screens {routes}: model call {attempt}/{_CALL_ATTEMPTS} failed ({reason[:200]}).",
                meta={
                    "routes": [route["route"] for route in batch],
                    "attempt": attempt,
                },
                duration_ms=timer.ms(),
            )
            continue
        emit(
            "info",
            "flowmap",
            f"Described {len(described)} screen(s): {routes}.",
            meta={"routes": [route["route"] for route in batch]},
            duration_ms=timer.ms(),
        )
        return described
    return None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _merge_actions(
    found: list[dict[str, str]], described: object, known: set[str]
) -> list[dict[str, str | None]]:
    actions: list[dict[str, str | None]] = []
    labelled: set[str] = set()
    for item in described if isinstance(described, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            continue
        target = item.get("to")
        to = target if isinstance(target, str) and target in known else None
        actions.append({"label": item["label"].strip(), "to": to})
        if to:
            labelled.add(to)
    for link in found:
        if link["to"] not in labelled:
            actions.append({"label": link["label"], "to": link["to"]})
            labelled.add(link["to"])
    return actions


def _attach_tree(nodes: dict[str, dict[str, Any]]) -> None:
    """Breadth-first from the root, so each screen records the shortest way in."""
    for node in nodes.values():
        node["parent"] = None
        node["children"] = []
        node["entry"] = []
        node["reachable"] = False
    if ROOT not in nodes:
        return
    nodes[ROOT]["reachable"] = True
    queue: deque[str] = deque([ROOT])
    while queue:
        current = queue.popleft()
        for action in nodes[current].get("actions", []):
            target = action.get("to")
            if not target or target not in nodes or nodes[target]["reachable"]:
                continue
            child = nodes[target]
            child["reachable"] = True
            child["parent"] = current
            child["entry"] = [
                *nodes[current]["entry"],
                {"from": current, "label": action.get("label") or "", "to": target},
            ]
            nodes[current]["children"].append(target)
            queue.append(target)
    for key, node in nodes.items():
        if key != ROOT and not node["reachable"]:
            nodes[ROOT]["children"].append(key)


def _embedding_text(node: dict[str, Any]) -> str:
    labels = " ".join(
        str(action.get("label") or "") for action in node.get("actions", [])
    )
    return "\n".join(
        [
            str(node.get("name", "")),
            str(node.get("route", "")),
            str(node.get("summary", "")),
            " ".join(node.get("fields", [])),
            labels,
        ]
    ).strip()


def file_hashes(embeddings_dir: Path) -> dict[str, str]:
    """Content hash of every indexed file, from the embeddings manifest."""
    manifest = load_manifest(embeddings_dir) or {}
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        path: str(entry.get("content_hash"))
        for path, entry in files.items()
        if isinstance(entry, dict) and entry.get("content_hash")
    }


def build_flow_map(
    cfg: TursorWorkspaceConfig,
    records: list[StoredChunkRecord],
    *,
    describe: bool = True,
    previous: dict[str, Any] | None = None,
    hashes: dict[str, str] | None = None,
    openai_base_url: str = "https://api.openai.com/v1",
) -> dict[str, Any]:
    """Routes and links always come from the code. A screen's model description is
    reused from `previous` when none of its files changed, so a save only sends the
    screens it touched to the model."""
    texts = _grouped_text(records)
    routes = extract_routes(texts)
    known_routes = [route["route"] for route in routes]
    patterns = [(route, _route_pattern(route)) for route in known_routes]
    known = set(known_routes)
    hashes = hashes or {}
    previous_nodes = (previous or {}).get("nodes") or {}

    screen_files = {route["route"]: _screen_files(route, texts) for route in routes}
    dep_hashes = {
        key: {path: hashes.get(path, "") for path in files}
        for key, files in screen_files.items()
    }
    described: dict[str, dict[str, Any]] = {}
    for key in known_routes:
        old = previous_nodes.get(key)
        if (
            isinstance(old, dict)
            and isinstance(old.get("model"), dict)
            and old.get("dep_hashes") == dep_hashes[key]
        ):
            described[key] = old["model"]
    to_describe = [route for route in routes if route["route"] not in described]

    emit(
        "info",
        "flowmap",
        f"Found {len(routes)} routes in the code.",
        meta={"routes": known_routes[:50]},
    )
    failed_batches = 0
    if described:
        emit(
            "info",
            "flowmap",
            f"Reusing descriptions of {len(described)} unchanged screen(s); "
            f"{len(to_describe)} screen(s) changed or new.",
        )
    if describe and cfg.ai is not None and to_describe:
        batches = _batches(to_describe, texts)
        emit(
            "info",
            "flowmap",
            f"Asking the model to describe {len(to_describe)} screen(s) in "
            f"{len(batches)} batch(es), {_PARALLEL_CALLS} at a time.",
        )
        with (
            httpx.Client(timeout=_CALL_TIMEOUT) as client,
            ThreadPoolExecutor(max_workers=_PARALLEL_CALLS) as pool,
        ):

            def describe_one(
                batch: list[dict[str, Any]],
            ) -> dict[str, dict[str, Any]] | None:
                return _describe_batch_safely(
                    cfg, batch, texts, known_routes, client, openai_base_url
                )

            # Each worker runs in its own copy of the request context, so logs keep the trace.
            futures = [
                pool.submit(contextvars.copy_context().run, describe_one, batch)
                for batch in batches
            ]
            for future in futures:
                result = future.result()
                if result is None:
                    failed_batches += 1
                else:
                    described.update(result)

    nodes: dict[str, dict[str, Any]] = {}
    for route in routes:
        key = route["route"]
        files = screen_files[key]
        found: list[dict[str, str]] = []
        for path in files:
            for link in extract_links(texts[path], patterns):
                if link["to"] != key and link not in found:
                    found.append(link)
        info = described.get(key, {})
        name = info.get("name")
        summary = info.get("summary")
        nodes[key] = {
            "route": key,
            "name": name.strip()
            if isinstance(name, str) and name.strip()
            else _default_name(route["component"], key),
            "summary": summary.strip() if isinstance(summary, str) else "",
            "file": route.get("file") or "",
            "dependencies": files,
            "requires_auth": any("auth" in guard.lower() for guard in route["guards"]),
            "guards": route["guards"],
            "fields": _strings(info.get("fields")),
            "errors": _strings(info.get("errors")),
            "actions": _merge_actions(found, info.get("actions"), known),
            # Kept so the next build can reuse this description while the files match.
            "model": info or None,
            "dep_hashes": dep_hashes[key],
        }

    _attach_tree(nodes)
    ordered = list(nodes.values())
    vectors = (
        embed_texts([_embedding_text(node) for node in ordered]) if ordered else []
    )
    for node, vector in zip(ordered, vectors, strict=True):
        node["embedding"] = vector
    return {
        "version": 1,
        "root": ROOT,
        "described": sum(1 for key in nodes if key in described),
        "failed_batches": failed_batches,
        "nodes": nodes,
    }


def write_flow_map(
    cfg: TursorWorkspaceConfig,
    records: list[StoredChunkRecord],
    *,
    openai_base_url: str = "https://api.openai.com/v1",
) -> dict[str, Any]:
    mock = os.environ.get("TURSOR_AI_MOCK_LLM", "").strip() in {"1", "true", "yes"}
    flow = build_flow_map(
        cfg,
        records,
        describe=not mock,
        previous=load_flow_map(cfg.embeddings_path),
        hashes=file_hashes(cfg.embeddings_path),
        openai_base_url=openai_base_url,
    )
    cfg.embeddings_path.mkdir(parents=True, exist_ok=True)
    write_atomic(map_path(cfg.embeddings_path), json.dumps(flow, indent=2))
    legacy = cfg.embeddings_path / LEGACY_MAP_FILE
    if legacy.is_file():
        legacy.unlink()
    return flow


def describe_entry(node: dict[str, Any]) -> list[str]:
    """Human steps from the first page to this screen."""
    steps = [f"Open {ROOT}"]
    if node.get("requires_auth"):
        steps.append("Sign in through the screens first; this screen needs an account")
    for hop in node.get("entry", []):
        label = hop.get("label") or "the control"
        steps.append(f'Click "{label}" to reach {hop.get("to")}')
    if not node.get("reachable", True):
        steps.append(
            f"No link from {ROOT} was found; it may need a sign-in or a direct address"
        )
    return steps


def public_node(node: dict[str, Any]) -> dict[str, Any]:
    """The view of a screen that is sent to the model and to the backend."""
    return {
        "module_key": node.get("route", ""),
        "name": node.get("name", ""),
        "summary": node.get("summary", ""),
        "requires_auth": bool(node.get("requires_auth")),
        "how_to_reach": describe_entry(node),
        "fields": node.get("fields", []),
        "actions": node.get("actions", []),
        "errors": node.get("errors", []),
        "dependencies": node.get("dependencies", []),
    }


def select_nodes(
    flow: dict[str, Any] | None,
    query: str,
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    usable = [
        node for node in flow_nodes(flow) if isinstance(node.get("embedding"), list)
    ]
    if not usable or not query.strip():
        return []
    query_vec = embed_texts([query.strip()])
    if not query_vec:
        return []
    ranked = sorted(
        usable,
        key=lambda node: _cosine_similarity(query_vec[0], node["embedding"]),
        reverse=True,
    )
    return [public_node(node) for node in ranked[:top_k]]


def dependency_chunks(
    records: list[StoredChunkRecord],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    paths: set[str] = set()
    for node in nodes:
        for path in node.get("dependencies") or []:
            if isinstance(path, str):
                paths.add(path)
    chunks: list[dict[str, Any]] = []
    for record in records:
        if record.path not in paths:
            continue
        chunks.append(
            {
                "path": record.path,
                "content": record.text,
                "start_line": record.start_line,
                "end_line": record.end_line,
            },
        )
    chunks.sort(key=lambda item: (str(item["path"]), int(item["start_line"])))
    return chunks
