"""Structured logs that reach the extension's log panel.

The backend sends two headers with each request: X-Tursor-Trace (one id per chat
turn, context build, run, or report) and X-Tursor-Log-Sink (its ingest URL). Events
logged while handling that request are queued and posted to the sink in small
batches from a background thread, so logging never slows a request down. They are
also written to this process's own log.

Never put API keys, prompt text, the conversation case, or typed values in a log.
"""

from __future__ import annotations

import contextvars
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("tursor_ai")

LEVELS = {"debug", "info", "warn", "error", "success"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_FLUSH_SECONDS = 0.25
_BATCH_SIZE = 50
_MAX_QUEUE = 2000


@dataclass(frozen=True)
class LogTarget:
    trace_id: str
    sink: str


_target: contextvars.ContextVar[LogTarget | None] = contextvars.ContextVar(
    "tursor_log_target", default=None
)
_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=_MAX_QUEUE)
_worker_started = False
_worker_lock = threading.Lock()


def _safe_sink(raw: str | None) -> str | None:
    """Only a loopback http URL may receive logs."""
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK:
        return None
    return raw


def bind_request(
    trace_id: str | None, sink: str | None
) -> contextvars.Token[LogTarget | None]:
    safe = _safe_sink(sink)
    target = LogTarget(trace_id[:80], safe) if trace_id and safe else None
    return _target.set(target)


def unbind_request(token: contextvars.Token[LogTarget | None]) -> None:
    _target.reset(token)


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_drain, name="tursor-log-relay", daemon=True).start()
        _worker_started = True


def _drain() -> None:
    with httpx.Client(timeout=3.0) as client:
        while True:
            sink, event = _queue.get()
            batches: dict[str, list[dict[str, Any]]] = {sink: [event]}
            deadline = time.monotonic() + _FLUSH_SECONDS
            while sum(len(items) for items in batches.values()) < _BATCH_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_sink, next_event = _queue.get(timeout=remaining)
                except queue.Empty:
                    break
                batches.setdefault(next_sink, []).append(next_event)
            for target, events in batches.items():
                try:
                    client.post(target, json={"events": events})
                except httpx.HTTPError:
                    # The panel misses these lines; the local log still has them.
                    pass


def emit(
    level: str,
    category: str,
    message: str,
    *,
    meta: dict[str, Any] | None = None,
    duration_ms: float | None = None,
) -> None:
    level = level if level in LEVELS else "info"
    python_level = {
        "debug": logging.DEBUG,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }.get(level, logging.INFO)
    logger.log(python_level, "[%s] %s", category, message)

    target = _target.get()
    if target is None:
        return
    event: dict[str, Any] = {
        "traceId": target.trace_id,
        "level": level,
        "category": category,
        "message": message[:2000],
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if meta:
        event["meta"] = meta
    if duration_ms is not None:
        event["durationMs"] = round(duration_ms)
    _ensure_worker()
    try:
        _queue.put_nowait((target.sink, event))
    except queue.Full:
        pass


class Timer:
    """Milliseconds since creation, for duration_ms."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000
