"""Command-line service entry point."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from .config import Settings, get_settings
from .observability import configure_logging


def _bootstrap_logging(log_path: Path) -> None:
    try:
        configure_logging(log_path.parent)
    except Exception as exc:
        sys.stderr.write(f"sandbox logging initialization failed: {type(exc).__name__}\n")


def _attach_telemetry_sink(log_path: Path, settings: Settings) -> None:
    """Give the running log pipeline the configured telemetry sink.

    `configure_logging` runs before the settings exist so that a configuration
    error is itself logged, and the sink selector lives in the settings, so it
    arrives here. Everything else about the pipeline is already in place.
    """
    try:
        configure_logging(log_path.parent, settings=settings)
    except Exception as exc:
        sys.stderr.write(f"sandbox telemetry sink setup failed: {type(exc).__name__}\n")


def _emit_startup_marker(log_path: Path, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except Exception:
        pass


def main() -> None:
    startup_log = Path(os.getenv("SANDBOX_STARTUP_LOG_PATH") or "/tmp/agent-sandbox-startup.log")
    _bootstrap_logging(startup_log)
    log = logging.getLogger("agent_sandbox.main")

    try:
        settings = get_settings()
        _attach_telemetry_sink(startup_log, settings)
        log.info(
            "agent-sandbox booting host=%s port=%s db_url_set=%s registry=%s",
            settings.host,
            settings.port,
            bool(settings.database_url),
            settings.registry_backend,
        )
        _emit_startup_marker(
            startup_log,
            f"agent-sandbox,status=starting,phase=uvicorn,host={settings.host},port={settings.port}",
        )
        uvicorn.run(
            "agent_sandbox.app:app",
            host=settings.host,
            port=settings.port,
            workers=1,
            log_config=None,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    except SystemExit:
        raise
    except BaseException as exc:
        log.exception("agent-sandbox startup crashed")
        _emit_startup_marker(
            startup_log,
            f"agent-sandbox,status=failed,phase=startup,exc={type(exc).__name__}",
        )
        sys.stdout.flush()
        sys.stderr.write(f"agent-sandbox startup crashed: {type(exc).__name__}\n")
        sys.stderr.flush()
        sys.exit(3)


if __name__ == "__main__":
    main()
