"""Egress policy for sandbox network access.

Sandboxes run untrusted agent code inside the same worker container as the
control plane, so "the sandbox can reach the network" and "the sandbox can
reach *this service*" are very different statements. This module answers the
second one.

The check here is the resolved-address check: an allowlist matches by *name*,
but whoever controls a permitted name's DNS decides what it resolves to. A
name that resolves to a loopback address, to this host's own LAN address, or
to a cloud instance-metadata endpoint is a way back into the trusted side of
the boundary, so the address a name resolves to is judged separately from the
name itself.

This is a reusable policy helper, NOT enforcement by the built-in execution
backend. A plugin must connect it to the actual dial path and prevent direct
network bypasses; exporting HTTP_PROXY alone does not do that. The built-in
backend refuses nonempty address-policy settings rather than ignoring them.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Instance-metadata and platform endpoints that sit outside link-local space,
# where a single request can return credentials for the host's cloud identity.
_METADATA_ADDRESSES: tuple[str, ...] = (
    "100.100.100.200",  # Alibaba Cloud
    "168.63.129.16",  # Azure platform
    "192.0.0.192",  # Oracle Cloud
    "fd00:ec2::254",  # AWS IMDS over IPv6
    "fd20:ce::254",
    "fd00:c1::a9fe:a9fe",
    "fd00:42::42",
)

_DENIED_NETWORKS: tuple[str, ...] = (
    "169.254.0.0/16",  # link-local, including 169.254.169.254
    "fe80::/10",
)


@dataclass(frozen=True, slots=True)
class AddressVerdict:
    """Why one address was refused.

    ``reason`` names the *class* of address rather than the address itself, so
    a denial can be reported to the agent without disclosing the topology it
    just probed.
    """

    address: str
    reason: str


class EgressPolicy:
    """Decide whether a sandbox may connect to a resolved address.

    ``extra_denied`` accepts addresses or CIDR ranges the deployment knows are
    sensitive but the runtime cannot discover: a cloud instance's 1:1-NAT
    public address, a router port-forward, or private ranges that hold
    infrastructure rather than the intranet services an agent legitimately
    needs.

    ``allowed_literals`` are addresses an operator listed on purpose. Allowing
    ``127.0.0.1:3000`` is an explicit choice, so it is honoured even though
    loopback is otherwise denied.
    """

    def __init__(
        self,
        *,
        extra_denied: Iterable[str] = (),
        allowed_literals: Iterable[str] = (),
        deny_host_addresses: bool = True,
    ) -> None:
        self._denied_networks: list[IPNetwork] = [
            ipaddress.ip_network(entry) for entry in _DENIED_NETWORKS
        ]
        self._denied_addresses: set[IPAddress] = {
            ipaddress.ip_address(entry) for entry in _METADATA_ADDRESSES
        }
        for entry in extra_denied:
            item = entry.strip()
            if not item:
                continue
            if "/" in item:
                self._denied_networks.append(ipaddress.ip_network(item, strict=False))
            else:
                self._denied_addresses.add(ipaddress.ip_address(item))
        self._allowed: set[IPAddress] = set()
        for entry in allowed_literals:
            item = entry.strip()
            if item:
                self._allowed.add(ipaddress.ip_address(item))
        self._host_addresses: frozenset[IPAddress] = (
            host_addresses() if deny_host_addresses else frozenset()
        )

    def check(self, address: str) -> AddressVerdict | None:
        """Return why ``address`` is refused, or ``None`` when it is allowed."""

        try:
            parsed = ipaddress.ip_address(address.strip().strip("[]"))
        except ValueError:
            return AddressVerdict(address, "not an IP address")

        candidate = _unwrap_ipv4(parsed)
        if candidate in self._allowed or parsed in self._allowed:
            return None

        for value in {parsed, candidate}:
            if value.is_loopback:
                return AddressVerdict(address, "resolved to a loopback address")
            if value.is_unspecified:
                return AddressVerdict(address, "resolved to an unspecified address")
            if value.is_multicast:
                return AddressVerdict(address, "resolved to a multicast address")
            if value.is_link_local:
                return AddressVerdict(address, "resolved to a link-local address")
            if value in self._denied_addresses:
                return AddressVerdict(address, "resolved to a denied address")
            if value in self._host_addresses:
                return AddressVerdict(address, "resolved to this host's own address")
            for network in self._denied_networks:
                if value.version == network.version and value in network:
                    return AddressVerdict(address, "resolved to a denied range")
        return None

    def filter(self, addresses: Sequence[str]) -> tuple[list[str], list[AddressVerdict]]:
        """Split resolved addresses into those that may be dialed and refusals."""

        allowed: list[str] = []
        refused: list[AddressVerdict] = []
        for address in addresses:
            verdict = self.check(address)
            if verdict is None:
                allowed.append(address)
            else:
                refused.append(verdict)
        return allowed, refused


def _unwrap_ipv4(address: IPAddress) -> IPAddress:
    """Return the IPv4 address an IPv6 form carries, when it carries one.

    IPv4-mapped, 6to4, and the well-known NAT64 prefix all embed an IPv4
    address that would otherwise escape an IPv4 rule. Network-specific NAT64
    prefixes are deliberately not decoded: RFC 6052 allows the IPv4 in several
    positions, so the layout cannot be recognised from the address alone.
    """

    if not isinstance(address, ipaddress.IPv6Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    packed = address.packed
    if packed[:12] == b"\x00\x64\xff\x9b" + b"\x00" * 8:  # 64:ff9b::/96
        return ipaddress.IPv4Address(packed[12:])
    return address


@lru_cache(maxsize=1)
def host_addresses() -> frozenset[IPAddress]:
    """Every address currently assigned to one of this host's interfaces.

    A service bound to ``0.0.0.0`` — this control plane included — answers on
    the host's LAN and public addresses exactly as it does on loopback, so
    those addresses are as sensitive as ``127.0.0.1``.

    Cached: each lookup can reach a resolver, and a worker's own addresses do
    not change within the lifetime of a process. Call
    ``host_addresses.cache_clear()`` after an interface change.
    """

    found: set[IPAddress] = set()
    try:
        hostname = socket.gethostname()
    except OSError:
        return frozenset()
    for candidate in (hostname, f"{hostname}.local", ""):
        try:
            infos = socket.getaddrinfo(candidate or None, None, proto=socket.IPPROTO_TCP)
        except (OSError, socket.gaierror):
            continue
        for info in infos:
            address = _sockaddr_host(info[4])
            if address is not None:
                found.add(address)
    return frozenset(found)


def _sockaddr_host(sockaddr: tuple[object, ...]) -> IPAddress | None:
    """Parse the host out of a ``getaddrinfo`` sockaddr.

    Only AF_INET and AF_INET6 carry a textual address in the first slot; other
    families (AF_UNIX, AF_PACKET) put something else there, so anything that is
    not a parsable address is skipped rather than coerced.
    """

    if not sockaddr:
        return None
    raw = sockaddr[0]
    if not isinstance(raw, str):
        return None
    try:
        # A link-local address arrives with a zone id (`fe80::1%en0`).
        return ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return None


def resolve_and_filter(
    hostname: str,
    policy: EgressPolicy,
    *,
    port: int = 0,
) -> tuple[list[str], list[AddressVerdict]]:
    """Resolve ``hostname`` once and apply ``policy`` to the answers.

    The surviving address is the one a caller must dial: resolving a second
    time would let a name that passed the check return a different answer.
    """

    try:
        infos = socket.getaddrinfo(hostname, port or None, proto=socket.IPPROTO_TCP)
    except (OSError, socket.gaierror):
        return [], [AddressVerdict(hostname, "name does not resolve")]
    addresses: list[str] = []
    for info in infos:
        address = _sockaddr_host(info[4])
        if address is None:
            continue
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        return [], [AddressVerdict(hostname, "name does not resolve")]
    return policy.filter(addresses)


__all__ = [
    "AddressVerdict",
    "EgressPolicy",
    "host_addresses",
    "resolve_and_filter",
]
