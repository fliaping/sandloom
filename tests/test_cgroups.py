from pathlib import Path

import pytest

from agent_sandbox.cgroups import (
    CgroupLimits,
    CgroupManager,
    CgroupUnavailable,
    placement_prefix,
)


def _manager(tmp_path: Path, **limits: object) -> CgroupManager:
    base = tmp_path / "base"
    (base / "sandboxes").mkdir(parents=True)
    defaults = {"cpu_cores": 1.5, "memory_bytes": 1024**3, "pids": 128}
    defaults.update(limits)
    manager = CgroupManager(CgroupLimits(**defaults), mount=tmp_path)  # type: ignore[arg-type]
    manager.base = base
    manager.active = True
    return manager


def test_sandbox_cgroup_carries_every_limit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    path = manager.ensure_sandbox("sb/1")
    assert path.name == "sb_1"
    assert (path / "cpu.max").read_text() == "150000 100000"
    assert (path / "memory.max").read_text() == str(1024**3)
    assert (path / "memory.swap.max").read_text() == "0"
    assert (path / "pids.max").read_text() == "128"
    assert (path / "cgroup.subtree_control").read_text() == "+cpu +memory +pids"


def test_unset_limits_are_not_written(tmp_path: Path) -> None:
    manager = _manager(tmp_path, cpu_cores=None, memory_bytes=None, pids=None)
    path = manager.ensure_sandbox("sb")
    assert not (path / "cpu.max").exists()
    assert not (path / "memory.max").exists()
    assert not (path / "pids.max").exists()


def test_execution_leaf_kills_as_a_group(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    leaf = manager.create_execution("sb", "exec-1")
    assert leaf.parent.name == "sb"
    assert (leaf / "memory.oom.group").read_text() == "1"
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n")
    assert manager.execution_stats(leaf)["memory.oom_kill"] == 1


def test_placement_joins_the_leaf_before_exec(tmp_path: Path) -> None:
    prefix = placement_prefix(tmp_path / "leaf")
    assert prefix[:2] == ["/bin/sh", "-c"]
    assert prefix[-1] == str(tmp_path / "leaf" / "cgroup.procs")


def test_setup_refuses_a_non_cgroup2_mount(tmp_path: Path) -> None:
    manager = CgroupManager(CgroupLimits(1, 1, 1), mount=tmp_path)
    with pytest.raises(CgroupUnavailable):
        manager.setup()
    assert manager.active is False
    assert "unavailable" in manager.detail
