"""HPSDR Protocol 1 discovery for Hermes Lite 2 / 2+.

Implements the public HPSDR Protocol 1 discovery handshake.
Board ID 6 = HermesLite family. HL2+ reports the same board ID; we
distinguish it later via gateware version / EEPROM config.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional

from lyra.protocol.netifaces import local_ipv4_addresses

DISCOVERY_PORT = 1024
DISCOVERY_PACKET_LEN = 63
BOARD_HERMES_LITE = 6

# Board IDs per the public HPSDR Protocol 1 device-type table
BOARD_NAMES = {
    0: "Atlas",
    1: "Hermes",
    2: "HermesII",
    4: "Angelia",
    5: "Orion",
    6: "HermesLite",
    10: "OrionMKII",
}


@dataclass
class RadioInfo:
    ip: str
    mac: str
    board_id: int
    board_name: str
    code_version: int
    is_busy: bool
    # HL2-specific extras (from MI0BOT fork)
    ee_config: int = 0
    ee_config_reserved: int = 0
    fixed_ip_hl2: str = ""
    num_rxs: int = 0
    beta_version: int = 0
    metis_version: int = 0


def _build_discovery_packet_p1() -> bytes:
    pkt = bytearray(DISCOVERY_PACKET_LEN)
    pkt[0] = 0xEF
    pkt[1] = 0xFE
    pkt[2] = 0x02
    return bytes(pkt)


def _parse_reply(data: bytes, sender_ip: str) -> Optional[RadioInfo]:
    if len(data) < 24:
        return None
    if data[0] != 0xEF or data[1] != 0xFE:
        return None
    status = data[2]
    if status not in (0x02, 0x03):
        return None

    mac = ":".join(f"{b:02X}" for b in data[3:9])
    code_version = data[9]
    board_id = data[10]
    is_busy = status == 0x03

    info = RadioInfo(
        ip=sender_ip,
        mac=mac,
        board_id=board_id,
        board_name=BOARD_NAMES.get(board_id, f"Unknown({board_id})"),
        code_version=code_version,
        is_busy=is_busy,
    )

    # HL2 extras from MI0BOT fork (bytes 11..16)
    if board_id == BOARD_HERMES_LITE:
        info.ee_config = data[11]
        info.ee_config_reserved = data[12]
        fixed_ip = bytes(reversed(data[13:17]))
        info.fixed_ip_hl2 = ".".join(str(b) for b in fixed_ip)

    if len(data) > 20:
        info.metis_version = data[19]
        info.num_rxs = data[20]

    # HL2-specific layout (MI0BOT): num_rxs lives at [19], beta at [21].
    # Override the generic Metis assignment above.
    if board_id == BOARD_HERMES_LITE:
        if len(data) > 19:
            info.num_rxs = data[19]
        if len(data) > 21:
            info.beta_version = data[21]

    return info


def discover(
    timeout_s: float = 1.5,
    attempts: int = 2,
    local_bind: str = "0.0.0.0",
    target_ip: Optional[str] = None,
    debug_log: Optional[list] = None,
) -> List[RadioInfo]:
    """Broadcast a Protocol 1 discovery and collect replies.

    With default arguments, fans out across **every local IPv4 NIC**
    in parallel — important on multi-NIC hosts where the OS would
    otherwise route a 255.255.255.255 broadcast out only one interface.
    Pass an explicit ``local_bind`` (other than ``"0.0.0.0"``) or a
    ``target_ip`` to keep the older single-socket behavior.

    Args:
        timeout_s: total wall time to wait for replies per attempt.
        attempts: how many times to resend the discovery packet.
        local_bind: local IP to bind the socket to. The default
            behavior changed:
            - "0.0.0.0" (default) → broadcast on EVERY local
              interface in turn (fixes the multi-NIC blind spot)
            - any specific IP → broadcast only from that interface
        target_ip: if set, unicast to this IP instead of broadcasting.
        debug_log: optional list to append diagnostic strings into.
            Used by the Network Discovery Probe dialog to show the
            operator exactly which interfaces were tried, what
            packets went out, what came back, etc.
    """
    found: dict[str, RadioInfo] = {}

    def _log(msg: str):
        if debug_log is not None:
            debug_log.append(msg)

    # Decide which interfaces to broadcast through.
    # Unicast (target_ip set): single socket bound to 0.0.0.0,
    # let the OS route normally to the target.
    # Multi-NIC broadcast: enumerate all local IPv4 addrs (via the
    # shared `lyra.protocol.netifaces` helper, which P2 discovery
    # also uses), bind a socket to each one, broadcast through each.
    # Specific bind IP: just use that one (operator override).
    if target_ip:
        bind_ips = ["0.0.0.0"]
        _log(f"Mode: unicast to {target_ip}")
    elif local_bind == "0.0.0.0":
        bind_ips = local_ipv4_addresses()
        # netifaces returns ["0.0.0.0"] as a fallback if enumeration
        # yields nothing usable, so bind_ips is always non-empty.
        _log(f"Mode: broadcast on {len(bind_ips)} local interface(s):"
             f" {', '.join(bind_ips)}")
    else:
        bind_ips = [local_bind]
        _log(f"Mode: broadcast from operator-specified bind IP {local_bind}")

    packet = _build_discovery_packet_p1()
    destination = target_ip if target_ip else "255.255.255.255"

    sockets: list[socket.socket] = []
    try:
        # Open one socket per bind IP. Set broadcast + reuse so
        # multiple sockets on the same port don't collide.
        for bind_ip in bind_ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((bind_ip, 0))
                s.settimeout(0.1)
                sockets.append(s)
                _log(f"  socket bound to {bind_ip}:{s.getsockname()[1]}")
            except OSError as e:
                _log(f"  could NOT bind to {bind_ip}: {e}")

        if not sockets:
            _log("ERROR: no sockets could be bound; discovery aborted")
            return []

        for attempt in range(attempts):
            _log(f"Attempt {attempt + 1}/{attempts}: sending {len(packet)}-byte "
                 f"discovery packet to {destination}:{DISCOVERY_PORT}")
            for s in sockets:
                try:
                    s.sendto(packet, (destination, DISCOVERY_PORT))
                except OSError as e:
                    _log(f"  send via {s.getsockname()[0]} failed: {e}")
            # Listen on EVERY socket for the full deadline window.
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                for s in sockets:
                    try:
                        data, addr = s.recvfrom(2048)
                    except socket.timeout:
                        continue
                    except ConnectionResetError:
                        # Windows surfaces ICMP "port unreachable" from
                        # a previous send as ECONNRESET on recvfrom.
                        # Means nobody answered on this socket; skip it
                        # and keep polling the others.
                        continue
                    except OSError:
                        continue
                    _log(f"  reply from {addr[0]}: {len(data)} bytes "
                         f"(via socket bound to {s.getsockname()[0]})")
                    info = _parse_reply(data, addr[0])
                    if info and info.mac not in found:
                        found[info.mac] = info
                        _log(f"    parsed: MAC={info.mac} board={info.board_name} "
                             f"busy={info.is_busy}")
                    elif info is None:
                        _log(f"    NOT a valid HPSDR reply (header mismatch / too short)")
    finally:
        for s in sockets:
            try: s.close()
            except OSError: pass

    _log(f"Discovery complete: {len(found)} radio(s) found")
    return list(found.values())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HL2 Protocol 1 discovery")
    parser.add_argument("--target", help="Unicast target IP (skip broadcast)")
    parser.add_argument("--bind", default="0.0.0.0", help="Local bind IP")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--attempts", type=int, default=2)
    args = parser.parse_args()

    radios = discover(
        timeout_s=args.timeout,
        attempts=args.attempts,
        local_bind=args.bind,
        target_ip=args.target,
    )
    if not radios:
        print("No radios found.")
    for r in radios:
        print(f"{r.ip:15s}  {r.mac}  {r.board_name}  "
              f"gateware=v{r.code_version}.{r.beta_version}  "
              f"busy={r.is_busy}  rxs={r.num_rxs}")
        if r.board_id == BOARD_HERMES_LITE:
            print(f"    HL2 fixed-IP setting: {r.fixed_ip_hl2}  "
                  f"ee_config=0x{r.ee_config:02X}")
