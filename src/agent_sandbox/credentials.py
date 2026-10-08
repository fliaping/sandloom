"""Credential injection for sandboxed execution environments.

Private packages can register credential broker factories to inject ephemeral
credentials, configure Git helpers, and validate sensitive environment variables
without storing tokens in workspace files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from .plugins import create_from_plugin

if TYPE_CHECKING:
    from .config import Settings
    from .schemas import ExecRequest

CREDENTIAL_BROKER_GROUP = "agent_sandbox.credential_brokers"


class CredentialBroker(Protocol):
    """Configure and validate the credentials one execution may use.

    A broker runs in the service process, once per execution, and is the
    deployment's say over what a caller may ask for: it is asked to allow the
    sensitive keys a request names, and what it returns becomes part of the
    environment the command runs with.
    """

    def augment_environment(
        self, request: ExecRequest, base_env: dict[str, str]
    ) -> dict[str, str]:
        """Return the environment this execution should run with.

        Args:
            request: The execution request, including its `sensitive_env`.
            base_env: The environment the caller attached to this request. The
                runtime adds `HOME`, `PATH`, the language toolchains and the
                proxy settings around it, and those are reserved: returning one
                from here fails the execution rather than silently overriding
                what the sandbox was configured with.

        Returns:
            The environment to run with. May include:
            - GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_*
            - Credential helper environment variables
            - Other process-scoped configuration

        Must NOT return sensitive values directly in plain environment
        variables: a variable is readable by every process in the sandbox and
        survives in `/proc`. Point at a helper instead, and read the values from
        `request.sensitive_env` there.
        """
        ...

    def validate_sensitive_keys(self, keys: set[str]) -> None:
        """Validate that all keys in ExecRequest.sensitive_env are allowed.
        
        Args:
            keys: Set of environment variable names from request.sensitive_env.
        
        Raises:
            ValueError: If any key is not in the broker's allowed set.
        """
        ...


class PassthroughCredentialBroker:
    """Default no-op broker that allows no sensitive environment variables."""

    def augment_environment(
        self, request: ExecRequest, base_env: dict[str, str]
    ) -> dict[str, str]:
        return base_env

    def validate_sensitive_keys(self, keys: set[str]) -> None:
        if keys:
            raise ValueError(
                f"sensitive environment variables not supported without a credential broker: {keys}"
            )


def create_credential_broker(settings: Settings) -> CredentialBroker:
    """Load the configured credential broker, or the one that refuses secrets.

    The selector is read from the settings here rather than taken as an
    argument, the way every other boundary factory does it: a caller that can
    name a different backend than the settings describe is a caller that can
    install a broker nothing else in the deployment knows about.
    """

    backend = settings.credential_broker
    if backend == "disabled":
        return PassthroughCredentialBroker()
    return cast(
        "CredentialBroker",
        create_from_plugin(CREDENTIAL_BROKER_GROUP, backend, settings),
    )


__all__ = [
    "CREDENTIAL_BROKER_GROUP",
    "CredentialBroker",
    "PassthroughCredentialBroker",
    "create_credential_broker",
]
