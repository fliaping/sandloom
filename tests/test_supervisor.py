import signal
from pathlib import Path

import pytest

from agent_sandbox import supervisor as sup
from agent_sandbox.supervisor import DISK, MEMORY, SandboxSupervisor, disk_usage


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(sup.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def _watch(supervisor: SandboxSupervisor, tmp_path: Path) -> None:
    supervisor.begin("sb", 30001, tmp_path, "exec-1")


def test_memory_is_summed_across_processes_and_kills_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    killed: list[int] = []
    supervisor = SandboxSupervisor(
        memory_bytes=300, cpu_cores=None, disk_bytes=None, kill_uid=killed.append
    )
    _watch(supervisor, tmp_path)
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: [11, 12, 13], 0: [1]})
    # Each process is under the limit; the sandbox is not.
    monkeypatch.setattr(sup, "process_memory", lambda pid: 120)
    supervisor._tick(0.1, 1.0)
    assert {pid for pid, sig in signals if sig == signal.SIGKILL} == {11, 12, 13}
    assert killed == [30001]
    assert supervisor.end("sb", "exec-1") == MEMORY


def test_under_the_memory_limit_nothing_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    supervisor = SandboxSupervisor(memory_bytes=1000, cpu_cores=None, disk_bytes=None)
    _watch(supervisor, tmp_path)
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: [11, 12]})
    monkeypatch.setattr(sup, "process_memory", lambda pid: 100)
    supervisor._tick(0.1, 1.0)
    assert signals == []
    assert supervisor.end("sb", "exec-1") is None


def test_cpu_over_quota_is_stopped_then_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    supervisor = SandboxSupervisor(memory_bytes=None, cpu_cores=1.0, disk_bytes=None)
    _watch(supervisor, tmp_path)
    ticks = {11: 0, 12: 0}
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: [11, 12]})
    monkeypatch.setattr(sup, "process_cpu_ticks", lambda pid: (ticks[pid], 0))
    supervisor._tick(0.1, 1.0)
    # Three CPU-seconds in one second against a one-core quota and burst.
    ticks.update({11: int(sup._TICKS * 1.5), 12: int(sup._TICKS * 1.5)})
    supervisor._tick(1.0, 2.0)
    assert {pid for pid, sig in signals if sig == signal.SIGSTOP} == {11, 12}
    signals.clear()
    # Stopped processes use nothing; the bucket refills and they continue.
    supervisor._tick(1.0, 3.0)
    supervisor._tick(1.0, 4.0)
    assert {pid for pid, sig in signals if sig == signal.SIGCONT} == {11, 12}


def test_reaped_child_time_is_not_charged_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    supervisor = SandboxSupervisor(memory_bytes=None, cpu_cores=1.0, disk_bytes=None)
    _watch(supervisor, tmp_path)
    state = {"pids": [11, 12], 11: (0, 0), 12: (int(sup._TICKS * 0.5), 0)}
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: state["pids"]})
    monkeypatch.setattr(sup, "process_cpu_ticks", lambda pid: state[pid])
    supervisor._tick(1.0, 1.0)
    budget = supervisor._watched["sb"].budget
    # The child exits; its half second moves into the parent's cutime.
    state["pids"] = [11]
    state[11] = (0, int(sup._TICKS * 0.5))
    supervisor._tick(0.0, 1.0)
    assert supervisor._watched["sb"].budget == pytest.approx(budget)


def test_io_bytes_are_duty_cycled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    supervisor = SandboxSupervisor(
        memory_bytes=None, cpu_cores=None, disk_bytes=None, io_write_bps=1000
    )
    _watch(supervisor, tmp_path)
    written = {11: 0}
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: [11]})
    monkeypatch.setattr(sup, "process_io", lambda pid: (0, written[pid]))
    supervisor._tick(0.1, 1.0)
    written[11] = 5000
    supervisor._tick(1.0, 2.0)
    assert (11, signal.SIGSTOP) in signals


def test_disk_growth_over_quota_kills_and_deletion_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    supervisor = SandboxSupervisor(
        memory_bytes=None, cpu_cores=None, disk_bytes=4096 * 4, disk_interval=0
    )
    _watch(supervisor, tmp_path)
    monkeypatch.setattr(sup, "processes_by_uid", lambda: {30001: [11]})
    monkeypatch.setattr(sup, "by_uid_for", lambda uid: [11])
    (tmp_path / "big").write_bytes(b"x" * 4096 * 8)
    supervisor._tick(0.1, 1.0)
    assert supervisor.end("sb", "exec-1") == DISK
    supervisor.begin("sb", 30001, tmp_path, "exec-2")
    signals.clear()
    (tmp_path / "big").write_bytes(b"x" * 4096 * 6)  # shrinking, still over
    supervisor._tick(0.1, 2.0)
    assert signals == []


def test_disk_usage_counts_allocated_blocks(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x" * 10000)
    (tmp_path / "link").symlink_to("/usr")
    assert 10000 <= disk_usage(tmp_path) < 10000 + 64 * 1024
