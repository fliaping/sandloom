"""The fleet verifier reads the periods back from the workers it drives.

`scripts/verify-fleet.py --short-graces` asserts that the fleet was started with
the shortened idle and grace periods, because every check that waits for a
reaper can only pass for the right reason when it was. That assertion is worth
nothing unless `/healthz` reports the *effective* periods, so the join between
the two is pinned here: the endpoint has to echo the settings it was built with,
and every period the script constrains has to be one the endpoint reports.

Skipping the exports in the documented command is an easy and silent mistake —
every other check still passes, and the idle check fails two minutes later
blaming the fleet for something the flag already claimed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-fleet.py"
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _load_verifier() -> Any:
    """Import the script as a module; its `__main__` guard keeps it from running."""

    spec = importlib.util.spec_from_file_location("verify_fleet", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _health(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    settings = Settings(
        internal_token=TOKEN,
        local_root=tmp_path,
        min_free_bytes=0,
        advertise_host="worker.test",
        **overrides,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker.test") as client:
        response = await client.get("/healthz", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["worker"]


async def test_health_reports_the_periods_this_deployment_runs_with(tmp_path: Path) -> None:
    """The defaults are not the answer: a fleet is verified as it was started."""

    worker = await _health(
        tmp_path,
        maintenance_interval_seconds=2,
        heartbeat_ttl_seconds=10,
        idle_ttl_seconds=45,
        orphan_release_grace_seconds=10,
        orphan_running_grace_seconds=7,
    )

    assert worker["reclamation"] == {
        "maintenance_interval_seconds": 2,
        "heartbeat_interval_seconds": 10,
        "heartbeat_ttl_seconds": 10,
        "idle_ttl_seconds": 45,
        "orphan_running_grace_seconds": 7,
        "orphan_release_grace_seconds": 10,
        "suspended_retention_seconds": 604800,
    }


async def test_every_period_the_verifier_constrains_is_one_health_reports(
    tmp_path: Path,
) -> None:
    """A rename on one side of this join must not become a silent skip."""

    verifier = _load_verifier()
    worker = await _health(tmp_path)

    unknown = sorted(set(verifier.SHORT_GRACE_LIMITS) - set(worker["reclamation"]))
    assert not unknown, f"the verifier constrains periods health does not report: {unknown}"


def test_the_limits_distinguish_a_shortened_fleet_from_a_shipped_one() -> None:
    """A limit the shipped default already satisfies proves nothing.

    These two are the reason the flag exists: at their shipped values the idle
    cleanup check cannot finish inside its window, and the worker-loss check
    waits out a five-minute release grace. The heartbeat TTL is deliberately not
    asserted here -- the shipped 30s is observable within the window, so that
    limit is a sanity bound rather than a discriminator.
    """

    verifier = _load_verifier()
    defaults = Settings(internal_token=TOKEN).model_dump()

    for name in ("idle_ttl_seconds", "orphan_release_grace_seconds"):
        assert name in verifier.SHORT_GRACE_LIMITS
        assert defaults[name] > verifier.SHORT_GRACE_LIMITS[name], (
            f"{name} ships at {defaults[name]}, which already satisfies the "
            f"{verifier.SHORT_GRACE_LIMITS[name]}s limit --short-graces enforces"
        )


class _HealthOnlyClient:
    """Answers /healthz with one worker record, and nothing else."""

    def __init__(self, worker: dict[str, Any]) -> None:
        self.worker = worker

    def ok(self, method: str, path: str) -> dict[str, Any]:
        assert (method, path) == ("GET", "/healthz"), (method, path)
        return {"worker": self.worker}


def _periods(**overrides: Any) -> dict[str, float]:
    values = {
        "maintenance_interval_seconds": 2,
        "heartbeat_ttl_seconds": 10,
        "idle_ttl_seconds": 45,
        "orphan_release_grace_seconds": 10,
        "suspended_retention_seconds": 604800,
    }
    values.update(overrides)
    return values


def test_the_preflight_accepts_a_worker_running_the_shortened_periods() -> None:
    verifier = _load_verifier()
    client = _HealthOnlyClient({"id": "worker-a-8080", "reclamation": _periods()})

    described = verifier.check_short_graces([client])

    assert "idle_ttl_seconds=45" in described


def test_the_preflight_refuses_a_worker_running_the_shipped_periods() -> None:
    """This is the mistake the check exists for: the exports left out of the
    documented command, and every other check still passing."""

    verifier = _load_verifier()
    shipped = Settings(internal_token=TOKEN).model_dump()
    client = _HealthOnlyClient(
        {
            "id": "worker-a-8080",
            "reclamation": {name: shipped[name] for name in verifier.SHORT_GRACE_LIMITS},
        }
    )

    with pytest.raises(verifier.Failure) as refused:
        verifier.check_short_graces([client])

    message = str(refused.value)
    assert "worker-a-8080" in message
    assert "idle_ttl_seconds" in message
    assert "OPEN_SOURCE_RELEASE.md" in message


def test_the_preflight_refuses_a_worker_that_does_not_report_the_periods() -> None:
    """An older image cannot be verified as one running the shortened periods."""

    verifier = _load_verifier()
    client = _HealthOnlyClient({"id": "worker-a-8080"})

    with pytest.raises(verifier.Failure, match="does not report its reclamation periods"):
        verifier.check_short_graces([client])
