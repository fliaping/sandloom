import os

import pytest


@pytest.fixture(autouse=True)
def _no_host_cgroups(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Unit tests must not create cgroups on the machine running them.

    The integration suite runs inside the worker image and exercises the real
    delegation; it keeps whatever SANDBOX_CGROUP_MODE its container sets.
    """
    if "integration" not in str(request.node.fspath) and "SANDBOX_CGROUP_MODE" not in os.environ:
        monkeypatch.setenv("SANDBOX_CGROUP_MODE", "off")
