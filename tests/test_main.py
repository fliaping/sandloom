"""The entry point, and the startup marker an operator reads to tell it started.

`python -m agent_sandbox.main` is the process every deployment runs, and it was
the one module the suite never imported: it reached 0% coverage while being
exercised exclusively by a live container. What it does is small, but two of its
behaviors are load-bearing — a marker line written before uvicorn starts, and an
exit code that says "I never came up" rather than a traceback an orchestrator
has to parse -- and neither had a test.

The marker is documented in CONFIGURATION.md as the way to tell a service that
started from one that never did, so the shape of the line is a published
interface, not an implementation detail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_sandbox import main as main_module
from agent_sandbox.config import Settings
from agent_sandbox.preflight import write_startup_marker

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path / "sandboxes",
        min_free_bytes=0,
        advertise_host="worker.test",
    )


class _Uvicorn:
    """Stands in for `uvicorn.run`, recording how it was called."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uvicorn: _Uvicorn, log: Path) -> None:
    monkeypatch.setenv("SANDBOX_STARTUP_LOG_PATH", str(log))
    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(tmp_path))
    # The real one installs a rotating file handler in a module global, which is
    # process-wide state a unit test has no business leaving behind.
    monkeypatch.setattr(main_module, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(main_module.uvicorn, "run", uvicorn)


def test_main_marks_the_start_and_then_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker is written before uvicorn is handed the port, so a crash
    during startup still leaves the line that says startup was attempted."""

    log = tmp_path / "startup.log"
    uvicorn = _Uvicorn()
    _prepare(monkeypatch, tmp_path, uvicorn, log)

    main_module.main()

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[-1].startswith("agent-sandbox,status=starting,phase=uvicorn,")
    assert f"port={_settings(tmp_path).port}" in lines[-1]

    assert len(uvicorn.calls) == 1
    (target,), kwargs = uvicorn.calls[0]
    assert target == "agent_sandbox.app:app"
    # One worker. The heartbeat and maintenance loops are asyncio tasks inside
    # the process, so a second uvicorn worker would run a second reaper.
    assert kwargs["workers"] == 1
    # Structured logging is the only pipeline: a uvicorn log config would send
    # the same records somewhere else as well.
    assert kwargs["log_config"] is None


def test_main_exits_three_with_a_marker_when_it_never_comes_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure has to be legible to whatever is watching the process.

    Exit code 3, a line in the same file the success marker goes to, and the
    exception named on stderr. An orchestrator restarts on a non-zero exit; what
    it cannot do is read a traceback.
    """

    log = tmp_path / "startup.log"
    uvicorn = _Uvicorn(raises=RuntimeError("address already in use"))
    _prepare(monkeypatch, tmp_path, uvicorn, log)

    with pytest.raises(SystemExit) as exit_info:
        main_module.main()

    assert exit_info.value.code == 3
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "agent-sandbox,status=failed,phase=startup,exc=RuntimeError"
    assert "startup crashed: RuntimeError" in capsys.readouterr().err


def test_a_logging_failure_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Logging is not a reason to refuse to serve, and its absence must be visible.

    `_bootstrap_logging` runs before the settings are parsed, so it cannot use a
    logger to say that it failed.
    """

    def explode(*_: Any, **__: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(main_module, "configure_logging", explode)

    main_module._bootstrap_logging(tmp_path / "startup.log")

    assert "sandbox logging initialization failed: OSError" in capsys.readouterr().err


def test_the_startup_marker_is_the_line_the_configuration_reference_publishes(
    tmp_path: Path,
) -> None:
    """The documented line and the written line are one string, joined here.

    CONFIGURATION.md tells an operator what to grep for. A service that appends
    a different line is a service whose readiness cannot be checked, and a
    return-code-free difference is exactly the kind no test notices.
    """

    reference = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    assert "agent-sandbox,status=success,ready,worker_id=" in reference, (
        "CONFIGURATION.md no longer publishes the marker line this test joins to"
    )

    path = tmp_path / "startup.log"
    write_startup_marker(path, "success", "ready,worker_id=agent-sandbox-8080,port=8080")
    write_startup_marker(path, "success", "ready,worker_id=agent-sandbox-8080,port=8080")

    assert path.read_text(encoding="utf-8").splitlines() == [
        "agent-sandbox,status=success,ready,worker_id=agent-sandbox-8080,port=8080",
        # Appended, not replaced: the file outlives the process, so it records
        # every time this host came up rather than only the last one.
        "agent-sandbox,status=success,ready,worker_id=agent-sandbox-8080,port=8080",
    ]
