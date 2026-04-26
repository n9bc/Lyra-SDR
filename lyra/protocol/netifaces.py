"""Enumerate local IPv4 interfaces for multi-NIC discovery broadcasts.

HPSDR discovery is a UDP limited broadcast (255.255.255.255) on port
1024. On a multi-NIC host — laptop with WiFi + dedicated HPSDR
Ethernet NIC, or a desktop with a virtual Hyper-V/WSL switch — the OS
picks ONE interface for that broadcast based on the routing table.
If the radio is not on the chosen interface, discovery turns up empty.

Fix: enumerate every local IPv4 address, bind one socket per address,
broadcast from each in parallel. Replies arrive on whichever socket's
interface the radio is actually reachable on.

Stdlib-only on purpose — keeps the discovery layer dependency-free
across Windows / Linux / macOS.
"""
from __future__ import annotations

import socket
from typing import List


def local_ipv4_addresses(include_loopback: bool = False) -> List[str]:
    """Return the IPv4 addresses bound to local interfaces.

    Order is the OS-reported order, deduped. Loopback (127.x) is
    excluded by default; pass ``include_loopback=True`` if you also
    want to broadcast onto the loopback interface (useful when running
    against ``tools/p2_loopback.py`` without an explicit ``--target``).

    Always returns at least one entry. If enumeration fails or yields
    nothing usable, falls back to ``["0.0.0.0"]`` so the caller can
    still bind something — that path matches the pre-multi-NIC
    behavior and is strictly no-worse-than-before.
    """
    seen: set[str] = set()
    addrs: list[str] = []

    try:
        info = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    except OSError:
        info = []

    for family, _type, _proto, _canon, sockaddr in info:
        if family != socket.AF_INET:
            continue
        ip = sockaddr[0]
        if ip in seen:
            continue
        if not include_loopback and ip.startswith("127."):
            continue
        seen.add(ip)
        addrs.append(ip)

    if not addrs:
        return ["0.0.0.0"]

    return addrs
