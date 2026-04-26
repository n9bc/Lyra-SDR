"""Lyra protocol layer: HPSDR Protocol 1 (Hermes Lite 2) + Protocol 2
(Apache ANAN family / Brick II).

The two protocols share UDP port 1024 for discovery, but their packet
formats are byte-distinguishable, so a single broadcast can elicit
replies from both at once. `discover_all()` runs both discoveries and
returns a unified list.

Direct imports (existing callers continue to work unchanged):

    from lyra.protocol.discovery import discover, RadioInfo  # P1
    from lyra.protocol.stream import HL2Stream                # P1
    from lyra.protocol.p2 import discover as discover_p2      # P2
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional, Union

from lyra.protocol import discovery as _p1
from lyra.protocol import p2 as _p2


@dataclass
class DiscoveredRadio:
    """Unified discovery result spanning P1 and P2 radios.

    `raw` carries the protocol-specific RadioInfo (P1 RadioInfo or
    P2RadioInfo) for callers that need the full field set. The top-level
    fields (ip, mac, board_id, board_name, code_version, is_busy) are
    common across both protocols, so most callers can ignore `raw`.
    """
    protocol: Literal["P1", "P2"]
    ip: str
    mac: str
    board_id: int
    board_name: str
    code_version: int
    is_busy: bool
    raw: Union[_p1.RadioInfo, _p2.P2RadioInfo]


def _wrap_p1(info: _p1.RadioInfo) -> DiscoveredRadio:
    return DiscoveredRadio(
        protocol="P1",
        ip=info.ip,
        mac=info.mac,
        board_id=info.board_id,
        board_name=info.board_name,
        code_version=info.code_version,
        is_busy=info.is_busy,
        raw=info,
    )


def _wrap_p2(info: _p2.P2RadioInfo) -> DiscoveredRadio:
    return DiscoveredRadio(
        protocol="P2",
        ip=info.ip,
        mac=info.mac,
        board_id=info.board_id,
        board_name=info.board_name,
        code_version=info.code_version,
        is_busy=info.is_busy,
        raw=info,
    )


def discover_all(
    timeout_s: float = 1.5,
    attempts: int = 2,
    local_bind: str = "0.0.0.0",
    target_ip: Optional[str] = None,
) -> List[DiscoveredRadio]:
    """Run P1 and P2 discovery and return the merged results.

    Discoveries are run sequentially (P1, then P2). Both share UDP 1024
    so the small extra delay is the safer choice over a single combined
    socket — keeps the parsers cleanly separated and avoids subtle
    race conditions on the bind.

    If a single physical radio replies to both protocols (theoretically
    possible if firmware supports both), the P2 entry wins. P2 is
    strictly newer and we'd rather drive newer protocol code paths.
    """
    p1 = _p1.discover(
        timeout_s=timeout_s,
        attempts=attempts,
        local_bind=local_bind,
        target_ip=target_ip,
    )
    p2 = _p2.discover(
        timeout_s=timeout_s,
        attempts=attempts,
        local_bind=local_bind,
        target_ip=target_ip,
    )

    by_mac: dict[str, DiscoveredRadio] = {}
    for info in p1:
        by_mac[info.mac] = _wrap_p1(info)
    # P2 entries overwrite P1 entries for the same MAC.
    for info in p2:
        by_mac[info.mac] = _wrap_p2(info)

    return list(by_mac.values())


__all__ = ["DiscoveredRadio", "discover_all"]
