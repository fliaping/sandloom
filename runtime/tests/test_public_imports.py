from agent_sandbox_runtime import IsolationLevel, SandboxRuntimeProfile
from agent_sandbox_runtime.bubblewrap import SandboxCommand
from agent_sandbox_runtime.isolation import negotiate_isolation


def test_public_package_reexports_runtime_api() -> None:
    profile = SandboxRuntimeProfile(isolation_level=IsolationLevel.STANDARD)

    assert profile.isolation_level is IsolationLevel.STANDARD
    assert SandboxCommand(argv=("/bin/true",), cwd="/", env={}).argv == ("/bin/true",)
    assert callable(negotiate_isolation)
