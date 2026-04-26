"""openHPSDR Protocol 2 discovery handshake.

Spec: openHPSDR Ethernet Protocol v4.4, "DISCOVERY PACKET" (host→radio)
and "DISCOVERY REPLY PACKET" (radio→host).

P2 shares UDP port 1024 with P1 for discovery. The two reply formats
are byte-distinguishable:

    P1 reply: starts with 0xEF 0xFE 0x02 (or 0x03), 60-ish byte length
    P2 reply: 60 bytes, byte[0..3] = seq# (0x00000000),
              byte[4] = 0x02 (idle) / 0x03 (busy) / 0xFE / 0xFF

So the high-level `lyra.protocol.discover_all()` aggregator can run both
discoveries in parallel and tag results by which parser accepted the
reply. This module only handles the P2 form.

Run from the command line:
    python -m lyra.protocol.p2.discovery
    python -m lyra.protocol.p2.discovery --target 192.168.1.50
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional

from lyra.protocol.p2.boards import lookup_board

DISCOVERY_PORT = 1024
DISCOVERY_PACKET_LEN = 60

# Discovery command code (byte 4 of host→radio packet)
CMD_DISCOVERY = 0x02

# Discovery reply status values (byte 4 of radio→host packet)
STATUS_IDLE = 0x02      # available
STATUS_RUNNING = 0x03   # already streaming to another host
STATUS_LONG_FORM = 0xFE         # XML hardware description follows
STATUS_LONG_FORM_RUNNING = 0xFF # XML follows + busy


@dataclass
class P2RadioInfo:
    """One P2-discovered radio.

    Mirrors the field set of the P1 `lyra.protocol.discovery.RadioInfo`
    where it makes sense (ip, mac, board_id, board_name, code_version,
    is_busy) and adds P2-specific fields (protocol_version, num_ddcs,
    metis_version, beta).
    """
    ip: str
    mac: str
    board_id: int
    board_name: str
    code_version: int                # firmware code version (byte 13), as raw int
    is_busy: bool
    # P2-specific
    protocol_version: int = 0        # byte 12, e.g. 104 → "v10.4"
    metis_version: int = 0           # byte 19
    num_ddcs: int = 0                # byte 20
    freq_or_phase: int = 0           # byte 21 (0 = freq, 1 = phase)
    endian_modes: int = 0            # byte 22 bitmask
    is_beta: bool = False            # byte 23
    # Atlas-only code versions, kept for completeness
    mercury_code_versions: tuple[int, int, int, int] = (0, 0, 0, 0)
    penny_code_version: int = 0
    # The long-form (XML/full) reply formats are detected but not parsed in v1.
    long_form: bool = False
    # Raw reply bytes — useful for debug / wireshark-style triage when
    # we see an unknown board ID. Capped at 60 bytes (the spec form).
    raw_reply: bytes = field(default=b"", repr=False)


def _build_discovery_packet() -> bytes:
    """Build the 60-byte P2 discovery request.

    Per spec: bytes 0..3 = sequence number (0x00000000), byte 4 = 0x02
    command, bytes 5..59 zero. Byte order on the wire is big-endian
    irrespective of the chosen Endian mode.
    """
    pkt = bytearray(DISCOVERY_PACKET_LEN)
    # bytes 0..3 already zero (seq)
    pkt[4] = CMD_DISCOVERY
    return bytes(pkt)


def _parse_reply(data: bytes, sender_ip: str) -> Optional[P2RadioInfo]:
    """Decode a P2 discovery reply, or return None if it doesn't look like one.

    The first sanity check rejects the P1 reply form (which starts with
    `0xEF 0xFE`) so a single shared socket could in principle hand
    bytes to both parsers without ambiguity.
    """
    if len(data) < 24:
        return None
    # Disambiguate from P1 reply, which starts 0xEF 0xFE 0x02.
    if data[0] == 0xEF and data[1] == 0xFE:
        return None
    # P2 reply has seq# = 0 in the first four bytes.
    if data[0] != 0 or data[1] != 0 or data[2] != 0 or data[3] != 0:
        return None
    status = data[4]
    if status not in (STATUS_IDLE, STATUS_RUNNING,
                      STATUS_LONG_FORM, STATUS_LONG_FORM_RUNNING):
        return None

    long_form = status in (STATUS_LONG_FORM, STATUS_LONG_FORM_RUNNING)
    is_busy = status in (STATUS_RUNNING, STATUS_LONG_FORM_RUNNING)

    mac = ":".join(f"{b:02X}" for b in data[5:11])
    board_id = data[11]
    spec = lookup_board(board_id)
    board_name = spec.short_name if spec is not None else f"Unknown(id={board_id})"

    info = P2RadioInfo(
        ip=sender_ip,
        mac=mac,
        board_id=board_id,
        board_name=board_name,
        code_version=data[13] if len(data) > 13 else 0,
        is_busy=is_busy,
        long_form=long_form,
        raw_reply=bytes(data[:DISCOVERY_PACKET_LEN]),
    )

    if len(data) > 12:
        info.protocol_version = data[12]
    if len(data) > 19:
        info.metis_version = data[19]
    if len(data) > 20:
        info.num_ddcs = data[20]
    if len(data) > 21:
        info.freq_or_phase = data[21]
    if len(data) > 22:
        info.endian_modes = data[22]
    if len(data) > 23:
        info.is_beta = bool(data[23])

    # Atlas-only fields. Only meaningful when board_id == 0; we read
    # them anyway so wireshark-style debugging has the values available.
    if len(data) > 18:
        info.mercury_code_versions = (
            data[14], data[15], data[16], data[17],
        )
        info.penny_code_version = data[18]

    return info


def discover(
    timeout_s: float = 1.5,
    attempts: int = 2,
    local_bind: str = "0.0.0.0",
    target_ip: Optional[str] = None,
) -> List[P2RadioInfo]:
    """Send a P2 discovery and collect replies.

    Args:
        timeout_s: total wall time to wait for replies per attempt.
        attempts: how many times to resend the discovery packet (helps
            recover from a single dropped UDP packet on noisy networks).
        local_bind: local IP to bind the sending socket to. 0.0.0.0
            covers all NICs.
        target_ip: if provided, unicast to this IP instead of broadcasting
            255.255.255.255. Useful when the radio has a fixed IP and
            broadcast is suppressed by the network.

    Returns:
        One P2RadioInfo per unique MAC that replied. Order is the order
        they replied (Python dict preserves insertion order).
    """
    found: dict[str, P2RadioInfo] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_bind, 0))
    sock.settimeout(0.1)

    packet = _build_discovery_packet()
    destination = target_ip if target_ip else "255.255.255.255"

    try:
        for _ in range(attempts):
            sock.sendto(packet, (destination, DISCOVERY_PORT))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                info = _parse_reply(data, addr[0])
                if info and info.mac not in found:
                    found[info.mac] = info
    finally:
        sock.close()

    return list(found.values())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="openHPSDR Protocol 2 discovery")
    parser.add_argument("--target", help="Unicast target IP (skip broadcast)")
    parser.add_argument("--bind", default="0.0.0.0", help="Local bind IP")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--raw", action="store_true",
                        help="Print raw 60-byte reply hex for each radio")
    args = parser.parse_args()

    radios = discover(
        timeout_s=args.timeout,
        attempts=args.attempts,
        local_bind=args.bind,
        target_ip=args.target,
    )
    if not radios:
        print("No P2 radios found.")
    for r in radios:
        proto_v = f"v{r.protocol_version // 10}.{r.protocol_version % 10}"
        print(
            f"{r.ip:15s}  {r.mac}  {r.board_name:18s}  "
            f"proto={proto_v}  fw={r.code_version}  "
            f"ddcs={r.num_ddcs}  busy={r.is_busy}"
            + ("  [beta]" if r.is_beta else "")
            + ("  [long-form reply, parser limited to header]" if r.long_form else "")
        )
        if args.raw:
            print("  raw:", " ".join(f"{b:02X}" for b in r.raw_reply))
