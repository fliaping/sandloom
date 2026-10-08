"""Structured logging for sandbox operations and HTTP requests.

The public package provides JSON formatting, event helpers, bounded queue delivery
to rotating log files, and a request-logging ASGI middleware. Deployments that need
enterprise telemetry backends can install a private package that registers a
TelemetrySink plugin factory.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import logging.handlers
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .plugins import TELEMETRY_SINK_GROUP, PluginLoadError, create_from_plugin

if TYPE_CHECKING:
    from collections.abc import Callable

    from .config import Settings

_FIELDS = frozenset(
    {
        "event",
        "level",
        "sandbox_id",
        "exec_id",
        "worker_id",
        "worker_epoch",
        "generation",
        "route",
        "method",
        "status_code",
        "error_code",
        "error_type",
        "outcome",
        "duration_ms",
        "queue_depth",
        "dropped",
        "delivery_failed",
        "sdk_accepted",
        "file_failed",
        "consumer_alive",
    }
)

_handler: AsyncServiceHandler | None = None
_CONFIG_LOCK = threading.Lock()


class JsonFormatter(logging.Formatter):
    """Structured JSON formatter for console output."""

    def format(self, record: logging.LogRecord) -> str:
        payload = safe_payload(record)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def safe_payload(record: logging.LogRecord) -> dict[str, Any]:
    """Extract allowlisted fields from a log record, treating the message as payload."""
    base: dict[str, Any] = {
        "timestamp": record.created,
        "level": record.levelname,
        "logger": record.name,
    }
    try:
        message = record.getMessage()
        msg_data = json.loads(message) if message else {}
    except (ValueError, TypeError):
        msg_data = {"message": record.getMessage()}
    for key in _FIELDS:
        if key in msg_data:
            base[key] = msg_data[key]
    if "message" in msg_data:
        base["message"] = msg_data["message"]
    if record.exc_info:
        base["exception"] = logging.Formatter().formatException(record.exc_info)
    return base


def event(name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event to the sandbox logger."""
    payload = {"event": name}
    payload.update(fields)
    logging.getLogger("agent_sandbox.events").log(level, json.dumps(payload, ensure_ascii=False))


def error_code(exc: BaseException) -> str:
    """Extract the sandbox error code from a RuntimeError or return the exception type."""
    if isinstance(exc, RuntimeError):
        return str(exc)
    return type(exc).__name__


class AsyncServiceHandler(logging.Handler):
    """Bounded queue delivery to rotating files and optional telemetry sink.

    Logs are written to {directory}/sandbox.log (INFO+) and {directory}/sandbox.err
    (ERROR+) with rotation. If a telemetry sink plugin is registered and enabled,
    log records are also delivered to that sink in a background thread.
    """

    def __init__(
        self,
        directory: Path,
        *,
        settings: Settings | None = None,
        normal_capacity: int = 2048,
        error_capacity: int = 512,
    ) -> None:
        super().__init__()
        self.directory = directory
        self.settings = settings
        self.normal: deque[logging.LogRecord] = deque(maxlen=normal_capacity)
        self.errors: deque[logging.LogRecord] = deque(maxlen=error_capacity)
        self.condition = threading.Condition()
        self.stopping = False
        self.dropped = 0
        self.delivery_failed = 0
        self.sdk_accepted = 0
        self.file_failed = 0
        self.thread = threading.Thread(target=self._drain, name="sandbox-log", daemon=True)
        self.thread.start()

    @property
    def sink_name(self) -> str | None:
        """The configured sink, for the health payload and the delivery loop."""

        return self.settings.telemetry_sink if self.settings else None

    def attach_telemetry(self, settings: Settings) -> None:
        """Point an already-running pipeline at the configured sink.

        `configure_logging` is installed before the settings exist — it runs
        first so that a configuration error is itself logged — so the selector
        arrives on a second call. The pipeline reads it when it next delivers,
        which is why this can be attached after the thread has started.
        """

        self.settings = settings

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread.ident:
            return
        payload = safe_payload(record)
        safe = logging.LogRecord(
            record.name,
            record.levelno,
            record.pathname,
            record.lineno,
            json.dumps(payload, ensure_ascii=False),
            (),
            None,
        )
        with self.condition:
            if self.stopping:
                return
            queue = self.errors if record.levelno >= logging.WARNING else self.normal
            if len(queue) == queue.maxlen:
                self.dropped += 1
            queue.append(safe)
            self.condition.notify()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "enabled": True,
                "sink_name": self.sink_name,
                "queue_depth": len(self.normal) + len(self.errors),
                "dropped": self.dropped,
                "delivery_failed": self.delivery_failed,
                "sdk_accepted": self.sdk_accepted,
                "file_failed": self.file_failed,
                "consumer_alive": self.thread.is_alive(),
            }

    def _drain(self) -> None:
        files: list[logging.Handler] = []
        delegate: logging.Handler | None = None
        retry_after = 0.0
        sink: logging.Handler
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for name, level in (("sandbox.log", logging.INFO), ("sandbox.err", logging.ERROR)):
                sink = RotatingFileHandler(
                    self.directory / name,
                    maxBytes=20 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                sink.setLevel(level)
                sink.setFormatter(logging.Formatter("%(message)s"))
                files.append(sink)
        except Exception:
            self.file_failed += 1
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopping or self.errors or self.normal)
                    if not self.errors and not self.normal:
                        return
                    record = (self.errors or self.normal).popleft()
                for sink in files:
                    if record.levelno >= sink.level:
                        try:
                            sink.handle(record)
                        except Exception:
                            self.file_failed += 1
                if self.sink_name is None or time.monotonic() < retry_after:
                    continue
                try:
                    if delegate is None:
                        assert self.settings is not None  # sink_name is set only with settings
                        delegate = _open_telemetry_sink(self.settings)
                        delegate.setFormatter(logging.Formatter("%(message)s"))
                    delegate.handle(record)
                    self.sdk_accepted += 1
                except Exception:
                    self.delivery_failed += 1
                    delegate = None
                    retry_after = time.monotonic() + 30
                    failure = logging.LogRecord(
                        "agent_sandbox.events",
                        logging.ERROR,
                        __file__,
                        0,
                        json.dumps(
                            {
                                "event": "telemetry_delivery_failed",
                                "delivery_failed": self.delivery_failed,
                            }
                        ),
                        (),
                        None,
                    )
                    for sink in files:
                        try:
                            sink.handle(failure)
                        except Exception:
                            self.file_failed += 1
        finally:
            for sink in files:
                sink.close()
            if delegate is not None:
                with contextlib.suppress(Exception):
                    delegate.close()

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)
        super().close()


def create_telemetry_sink(settings: Settings) -> Callable[[], logging.Handler] | None:
    """Load the configured telemetry sink, or None when none is configured.

    The entry point is a settings factory like every other plugin boundary: it
    takes the settings and returns the thing that is used, which here is a
    factory of its own — the handler is opened on the delivery thread so that a
    broken sink slows delivery rather than stopping the service from booting.
    """

    name = settings.telemetry_sink
    if not name:
        return None
    return cast(
        "Callable[[], logging.Handler]",
        create_from_plugin(TELEMETRY_SINK_GROUP, name, settings),
    )


def _open_telemetry_sink(settings: Settings) -> logging.Handler:
    """Open the configured sink, once, on the delivery thread's first record."""
    name = settings.telemetry_sink
    factory = create_telemetry_sink(settings)
    if factory is None:
        raise PluginLoadError("no telemetry sink is configured")
    if not callable(factory):
        raise PluginLoadError(
            f"telemetry sink {name!r} factory must return a callable handler factory"
        )
    handler = factory()
    if not isinstance(handler, logging.Handler):
        raise PluginLoadError(
            f"telemetry sink {name!r} factory must return a logging.Handler instance"
        )
    return handler


def logging_status() -> dict[str, Any]:
    return _handler.snapshot() if _handler else {"enabled": False}


def configure_logging(directory: Path, *, settings: Settings | None = None) -> None:
    """Initialize structured logging with rotating files and optional telemetry sink.

    Args:
        directory: Log file directory.
        settings: Where the telemetry sink selector is read from. The startup
            path installs logging before the settings exist and passes them on a
            second call; see `_bootstrap_logging`.
    """
    global _handler
    with _CONFIG_LOCK:
        if _handler is not None:
            # Already installed. A later call with settings is how the
            # configured telemetry sink reaches a pipeline that is already
            # running; everything else about it is in place.
            if settings is not None:
                _handler.attach_telemetry(settings)
            return
        handler = AsyncServiceHandler(directory, settings=settings)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        for name in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)
        root.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(JsonFormatter())
        root.addHandler(console)
        _handler = handler
        atexit.register(handler.close)
        event("logging_initialized", outcome="success")


class RequestLoggingMiddleware:
    """Pure ASGI observer; leaves request bodies and streaming responses untouched."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/health", "/healthz", "/metrics"}:
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500
        failure = ""

        async def observed_send(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            failure = type(exc).__name__
            raise
        finally:
            params = scope.get("path_params", {})
            event(
                "sandbox_http_completed",
                level=logging.WARNING if status >= 400 else logging.INFO,
                route=getattr(scope.get("route"), "path", "unmatched"),
                method=scope.get("method"),
                status_code=status,
                error_type=failure,
                sandbox_id=params.get("sandbox_id"),
                exec_id=params.get("exec_id"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )


__all__ = [
    "TELEMETRY_SINK_GROUP",
    "JsonFormatter",
    "RequestLoggingMiddleware",
    "configure_logging",
    "create_telemetry_sink",
    "error_code",
    "event",
    "logging_status",
    "safe_payload",
]
