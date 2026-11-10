import pytest
from fastapi.testclient import TestClient

from app import log_relay
from app.main import app


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(log_relay, "_ensure_worker", lambda: None)
    monkeypatch.setattr(log_relay._queue, "put_nowait", events.append)
    return events


def test_only_a_loopback_sink_receives_logs(captured) -> None:
    token = log_relay.bind_request("t1", "http://evil.example/ingest")
    log_relay.emit("info", "intent", "hello")
    log_relay.unbind_request(token)
    assert captured == []

    token = log_relay.bind_request("t1", "http://127.0.0.1:9090/logs/ingest")
    log_relay.emit("success", "intent", "routed", meta={"intent": "general_qa"}, duration_ms=12.4)
    log_relay.unbind_request(token)
    sink, event = captured[0]
    assert sink == "http://127.0.0.1:9090/logs/ingest"
    assert event["traceId"] == "t1"
    assert event["level"] == "success"
    assert event["meta"] == {"intent": "general_qa"}
    assert event["durationMs"] == 12


def test_request_headers_bind_the_trace(captured, tmp_path) -> None:
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text('{"excluded": []}', encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    res = TestClient(app).post(
        "/v1/embed",
        json={"directory_path": str(tmp_path)},
        headers={
            "X-Tursor-Trace": "trace-42",
            "X-Tursor-Log-Sink": "http://127.0.0.1:9090/logs/ingest",
        },
    )
    assert res.status_code == 200
    assert [event["traceId"] for _, event in captured] == ["trace-42"]
    assert captured[0][1]["category"] == "context"
