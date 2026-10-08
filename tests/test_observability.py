"""Structured logging and request observability tests."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.observability import (
    AsyncServiceHandler,
    JsonFormatter,
    configure_logging,
    error_code,
    event,
    safe_payload,
)


def test_json_formatter_structures_simple_message() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test.logger", logging.INFO, "test.py", 42, "test message", (), None)

    output = formatter.format(record)

    data = json.loads(output)
    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert "message" in data


def test_json_formatter_parses_json_message() -> None:
    formatter = JsonFormatter()
    payload = {"event": "test_event", "sandbox_id": "sandbox-123"}
    record = logging.LogRecord(
        "test.logger", logging.INFO, "test.py", 42, json.dumps(payload), (), None
    )

    output = formatter.format(record)

    data = json.loads(output)
    assert data["event"] == "test_event"
    assert data["sandbox_id"] == "sandbox-123"


def test_safe_payload_filters_allowlisted_fields() -> None:
    record = logging.LogRecord(
        "test.logger",
        logging.INFO,
        "test.py",
        42,
        json.dumps({"event": "test", "sandbox_id": "sb-1", "secret": "hidden"}),
        (),
        None,
    )

    payload = safe_payload(record)

    assert payload["event"] == "test"
    assert payload["sandbox_id"] == "sb-1"
    assert "secret" not in payload


def test_safe_payload_includes_exception_info() -> None:
    try:
        raise ValueError("test error")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test.logger", logging.ERROR, "test.py", 42, "error occurred", (), sys.exc_info()
        )

    payload = safe_payload(record)

    assert "exception" in payload
    assert "ValueError" in payload["exception"]
    assert "test error" in payload["exception"]


def test_event_helper_logs_structured_event() -> None:
    logger = logging.getLogger("agent_sandbox.events")
    handler = logging.handlers.MemoryHandler(capacity=10)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    event("test_event", level=logging.INFO, sandbox_id="sb-1", status_code=200)

    assert len(handler.buffer) == 1
    record = handler.buffer[0]
    data = json.loads(record.getMessage())
    assert data["event"] == "test_event"
    assert data["sandbox_id"] == "sb-1"
    assert data["status_code"] == 200


def test_error_code_extracts_runtime_error_message() -> None:
    exc = RuntimeError("SANDBOX_NOT_FOUND")

    code = error_code(exc)

    assert code == "SANDBOX_NOT_FOUND"


def test_error_code_returns_type_for_other_exceptions() -> None:
    exc = ValueError("some validation error")

    code = error_code(exc)

    assert code == "ValueError"


def test_async_handler_writes_to_rotating_files(tmp_path: Path) -> None:
    handler = AsyncServiceHandler(tmp_path)
    logger = logging.getLogger("test.async")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info(json.dumps({"event": "test_info"}))
    logger.error(json.dumps({"event": "test_error"}))
    time.sleep(0.1)

    handler.close()
    assert (tmp_path / "sandbox.log").exists()
    assert (tmp_path / "sandbox.err").exists()
    log_content = (tmp_path / "sandbox.log").read_text()
    err_content = (tmp_path / "sandbox.err").read_text()
    assert "test_info" in log_content
    assert "test_error" in err_content


def test_async_handler_snapshot_reports_queue_state(tmp_path: Path) -> None:
    handler = AsyncServiceHandler(tmp_path)

    snapshot = handler.snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["queue_depth"] >= 0
    assert snapshot["dropped"] == 0
    assert snapshot["consumer_alive"] is True

    handler.close()


def test_async_handler_does_not_recurse_on_own_thread(tmp_path: Path) -> None:
    handler = AsyncServiceHandler(tmp_path)
    logger = logging.getLogger("test.recursion")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Simulate emitting from the drain thread by setting thread ident.
    original_ident = handler.thread.ident
    record = logging.LogRecord("test.recursion", logging.INFO, "test.py", 42, "message", (), None)
    record.thread = original_ident

    handler.emit(record)

    snapshot = handler.snapshot()
    assert snapshot["queue_depth"] == 0

    handler.close()


def test_async_handler_bounds_queue_and_tracks_dropped(tmp_path: Path) -> None:
    handler = AsyncServiceHandler(tmp_path, normal_capacity=2)
    logger = logging.getLogger("test.bounded")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    for i in range(5):
        logger.info(json.dumps({"event": f"message_{i}"}))

    time.sleep(0.05)
    snapshot = handler.snapshot()
    assert snapshot["dropped"] >= 3

    handler.close()


def test_telemetry_sink_plugin_not_loaded_when_none(tmp_path: Path) -> None:
    handler = AsyncServiceHandler(tmp_path, settings=Settings())

    snapshot = handler.snapshot()

    assert snapshot["sink_name"] is None
    assert snapshot["sdk_accepted"] == 0

    handler.close()


class _EntryPoint:
    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self.value = value

    def load(self) -> Any:
        return self.value


def _only_entry_point(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    class EntryPoints(tuple):
        def select(self, *, name: str) -> Any:
            return EntryPoints(item for item in self if item.name == name)

    monkeypatch.setattr(
        "agent_sandbox.plugins.entry_points",
        lambda *, group: EntryPoints((_EntryPoint(name, value),)),
    )


class _RecordingSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_configured_telemetry_sink_actually_receives_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selector has to reach the pipeline, and the plugin has to be called.

    `configure_logging` ran before the settings existed and never passed the
    selector on, and the loader handed the plugin `PLUGIN_API_VERSION` where it
    expected the settings. Between them the feature was inert in both
    directions, which no test noticed because nothing asserted that a configured
    sink receives anything.
    """

    sink = _RecordingSink()
    seen: list[object] = []

    def settings_factory(settings: object) -> Any:
        seen.append(settings)
        return lambda: sink

    _only_entry_point(monkeypatch, "test-sink", settings_factory)
    settings = Settings(telemetry_sink="test-sink")
    handler = AsyncServiceHandler(tmp_path, settings=settings)
    try:
        handler.emit(
            logging.LogRecord("agent_sandbox.test", logging.INFO, "test.py", 1, "hello", (), None)
        )
        deadline = time.monotonic() + 5
        while not sink.records and time.monotonic() < deadline:
            time.sleep(0.02)

        assert seen == [settings], "the plugin was not given the settings it is configured by"
        assert [record.getMessage() for record in sink.records], "the sink received nothing"
        assert handler.snapshot()["sdk_accepted"] >= 1
    finally:
        handler.close()


def test_a_sink_that_fails_to_load_does_not_stop_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken telemetry plugin degrades delivery; it does not stop the service."""

    _only_entry_point(monkeypatch, "broken", lambda settings: (_ for _ in ()).throw(RuntimeError()))
    handler = AsyncServiceHandler(tmp_path, settings=Settings(telemetry_sink="broken"))
    try:
        handler.emit(
            logging.LogRecord("agent_sandbox.test", logging.ERROR, "test.py", 1, "boom", (), None)
        )
        deadline = time.monotonic() + 5
        while handler.snapshot()["delivery_failed"] == 0 and time.monotonic() < deadline:
            time.sleep(0.02)

        assert handler.snapshot()["delivery_failed"] >= 1
        assert (tmp_path / "sandbox.err").exists(), "the file sink stopped working"
    finally:
        handler.close()


def test_logging_attaches_a_sink_to_a_pipeline_that_is_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup order is fixed: logging first, settings second."""

    attached: list[Settings] = []

    class Running:
        def attach_telemetry(self, settings: Settings) -> None:
            attached.append(settings)

    monkeypatch.setattr("agent_sandbox.observability._handler", Running())
    settings = Settings(telemetry_sink="company-sink")

    configure_logging(Path("/tmp/unused-by-this-test"), settings=settings)

    assert attached == [settings]
