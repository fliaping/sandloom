"""Tests for the resolved-address egress check.

The threat these cover is specific to this architecture: the control plane and
the sandboxes share a worker container, so a name that resolves back to the
host is a path from a restricted workspace into the trusted side.
"""

from __future__ import annotations

import ipaddress

import pytest

from agent_sandbox.egress import EgressPolicy, host_addresses


def test_loopback_is_refused_by_class_not_by_address() -> None:
    policy = EgressPolicy(deny_host_addresses=False)

    verdict = policy.check("127.0.0.1")

    assert verdict is not None
    # The reason names the class of address. Reporting the address itself back
    # to the agent would confirm what it just probed.
    assert verdict.reason == "resolved to a loopback address"
    assert "127.0.0.1" not in verdict.reason


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # AWS/GCP IMDS
        "100.100.100.200",  # Alibaba Cloud
        "168.63.129.16",  # Azure
        "192.0.0.192",  # Oracle
    ],
)
def test_cloud_metadata_endpoints_are_refused(address: str) -> None:
    """These return credentials for the host's cloud identity in one request."""

    policy = EgressPolicy(deny_host_addresses=False)

    assert policy.check(address) is not None


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("0.0.0.0", "resolved to an unspecified address"),
        ("224.0.0.1", "resolved to a multicast address"),
        ("fe80::1", "resolved to a link-local address"),
        ("::1", "resolved to a loopback address"),
    ],
)
def test_non_routable_classes_are_refused(address: str, reason: str) -> None:
    policy = EgressPolicy(deny_host_addresses=False)
    verdict = policy.check(address)

    assert verdict is not None
    assert verdict.reason == reason


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",  # IPv4-mapped
        "2002:7f00:1::1",  # 6to4 carrying 127.0.0.1
        "64:ff9b::7f00:1",  # well-known NAT64 prefix
    ],
)
def test_ipv6_forms_that_embed_ipv4_are_judged_by_the_ipv4_address(address: str) -> None:
    """An IPv4 rule must not be escapable by writing the address in IPv6."""

    policy = EgressPolicy(deny_host_addresses=False)

    assert policy.check(address) is not None


def test_ipv4_deny_range_also_covers_its_ipv4_mapped_form() -> None:
    policy = EgressPolicy(extra_denied=["10.0.0.0/8"], deny_host_addresses=False)

    assert policy.check("10.1.2.3") is not None
    assert policy.check("::ffff:10.1.2.3") is not None


def test_a_routable_address_is_allowed() -> None:
    policy = EgressPolicy(deny_host_addresses=False)

    assert policy.check("93.184.216.34") is None


def test_an_explicitly_allowed_literal_overrides_its_class() -> None:
    """Allow-listing 127.0.0.1 is a deliberate choice for local development."""

    policy = EgressPolicy(allowed_literals=["127.0.0.1"], deny_host_addresses=False)

    assert policy.check("127.0.0.1") is None
    assert policy.check("127.0.0.2") is not None


def test_extra_denied_accepts_both_addresses_and_ranges() -> None:
    policy = EgressPolicy(
        extra_denied=["192.168.5.5", "172.16.0.0/12"],
        deny_host_addresses=False,
    )

    assert policy.check("192.168.5.5") is not None
    assert policy.check("172.20.1.1") is not None
    assert policy.check("192.168.5.6") is None


def test_a_non_address_is_refused_rather_than_allowed() -> None:
    policy = EgressPolicy(deny_host_addresses=False)
    verdict = policy.check("not-an-address")

    assert verdict is not None
    assert verdict.reason == "not an IP address"


def test_filter_splits_allowed_from_refused_and_keeps_order() -> None:
    policy = EgressPolicy(deny_host_addresses=False)

    allowed, refused = policy.filter(["93.184.216.34", "127.0.0.1", "8.8.8.8"])

    assert allowed == ["93.184.216.34", "8.8.8.8"]
    assert [entry.address for entry in refused] == ["127.0.0.1"]


def test_host_addresses_are_refused_because_a_bound_service_answers_on_them() -> None:
    """A control plane bound to 0.0.0.0 answers on the LAN address too."""

    discovered = host_addresses()
    if not discovered:
        pytest.skip("this host exposes no resolvable interface address")
    routable = [
        address
        for address in discovered
        if not (address.is_loopback or address.is_link_local or address.is_multicast)
    ]
    if not routable:
        pytest.skip("this host has only loopback and link-local addresses")

    policy = EgressPolicy()
    verdict = policy.check(str(routable[0]))

    assert verdict is not None
    assert verdict.reason == "resolved to this host's own address"


def test_host_address_denial_can_be_turned_off_for_a_split_deployment() -> None:
    """A control plane on another host makes its own addresses uninteresting."""

    discovered = [
        address
        for address in host_addresses()
        if not (address.is_loopback or address.is_link_local or address.is_multicast)
    ]
    if not discovered:
        pytest.skip("this host has no routable interface address")

    policy = EgressPolicy(deny_host_addresses=False)

    assert policy.check(str(discovered[0])) is None


def test_host_addresses_returns_parsed_addresses() -> None:
    for address in host_addresses():
        assert isinstance(address, ipaddress.IPv4Address | ipaddress.IPv6Address)
