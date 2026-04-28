"""Tests for the multi-NIC interface enumerator and the P2 discover()
fan-out dispatcher.

These tests don't touch the network — `socket.getaddrinfo` and the
per-interface `_discover_on` helper are monkey-patched.

P1 discovery's multi-NIC behavior is owned upstream (Lyra v0.0.4 added
its own monolithic implementation in `lyra/protocol/discovery.py`),
so it isn't tested here — `netifaces.local_ipv4_addresses` is the
shared seam that both protocols still go through.
"""
from __future__ import annotations

import socket
import unittest
from unittest import mock

from lyra.protocol import netifaces
from lyra.protocol.p2 import discovery as p2_disco


def _fake_addrinfo(ips: list[str]):
    """Build a getaddrinfo() return value for the given IPv4 list."""
    return [
        (socket.AF_INET, socket.SOCK_DGRAM, 0, "", (ip, 0))
        for ip in ips
    ]


class LocalIPv4AddressesTest(unittest.TestCase):
    def test_returns_loopback_excluded_by_default(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo",
            return_value=_fake_addrinfo(["127.0.0.1", "192.168.1.20"]),
        ):
            addrs = netifaces.local_ipv4_addresses()
        self.assertEqual(addrs, ["192.168.1.20"])

    def test_includes_loopback_when_requested(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo",
            return_value=_fake_addrinfo(["127.0.0.1", "10.0.0.5"]),
        ):
            addrs = netifaces.local_ipv4_addresses(include_loopback=True)
        self.assertEqual(sorted(addrs), ["10.0.0.5", "127.0.0.1"])

    def test_dedupes_repeated_ips(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo",
            return_value=_fake_addrinfo(
                ["192.168.1.20", "192.168.1.20", "10.0.0.5"]
            ),
        ):
            addrs = netifaces.local_ipv4_addresses()
        self.assertEqual(sorted(addrs), ["10.0.0.5", "192.168.1.20"])

    def test_falls_back_to_anyaddr_on_failure(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo", side_effect=OSError("nope"),
        ):
            addrs = netifaces.local_ipv4_addresses()
        self.assertEqual(addrs, ["0.0.0.0"])

    def test_falls_back_to_anyaddr_when_only_loopback(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo",
            return_value=_fake_addrinfo(["127.0.0.1"]),
        ):
            addrs = netifaces.local_ipv4_addresses()
        self.assertEqual(addrs, ["0.0.0.0"])


class P2DiscoverFanoutTest(unittest.TestCase):
    """The P2 discover() entrypoint must broadcast from every local IPv4
    when called with default args, and must NOT fan out when the caller
    passes an explicit local_bind or target_ip."""

    def test_p2_fans_out_across_interfaces(self) -> None:
        calls: list[tuple[str, object]] = []

        def fake_one(local_bind, target_ip, timeout_s, attempts, **_):
            calls.append((local_bind, target_ip))
            return []

        with mock.patch.object(
            p2_disco, "local_ipv4_addresses",
            return_value=["192.168.1.20", "10.0.0.5"],
        ), mock.patch.object(p2_disco, "_discover_on", side_effect=fake_one):
            p2_disco.discover()

        self.assertEqual(
            sorted(c[0] for c in calls),
            ["10.0.0.5", "192.168.1.20"],
        )
        for _, target in calls:
            self.assertIsNone(target)

    def test_explicit_target_ip_does_not_fan_out(self) -> None:
        with mock.patch.object(p2_disco, "_discover_on", return_value=[]) as m:
            p2_disco.discover(target_ip="127.0.0.1")
        self.assertEqual(m.call_count, 1)

    def test_explicit_local_bind_does_not_fan_out(self) -> None:
        with mock.patch.object(p2_disco, "_discover_on", return_value=[]) as m:
            p2_disco.discover(local_bind="192.168.1.20")
        self.assertEqual(m.call_count, 1)
        args, _ = m.call_args
        self.assertEqual(args[0], "192.168.1.20")


if __name__ == "__main__":
    unittest.main()
