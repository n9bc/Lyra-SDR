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

import threading
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
    debug_log: Optional[list] = None,
) -> List[DiscoveredRadio]:
    """Run P1 and P2 discovery and return the merged results.

    P1 and P2 run **in parallel** on separate threads. Each protocol
    binds its own ephemeral UDP sockets (different bound source ports),
    so the radios route their replies back to whichever protocol's
    socket actually sent the request — there is no contention on UDP
    1024 itself. Running them in parallel halves the wall-clock wait
    (was ~5 s sequential, now ~3 s = max(P1, P2)).

    If a single physical radio replies to both protocols (theoretically
    possible if firmware supports both), the P2 entry wins. P2 is
    strictly newer and we'd rather drive newer protocol code paths.

    `debug_log`, if supplied, accumulates per-protocol diagnostic
    strings (prefixed with [P1]/[P2]) so operator-facing tools (the
    Network Discovery Probe dialog, console-print fallbacks) can show
    exactly which interfaces were tried and what came back. Output
    order is P1-section then P2-section so the transcript stays
    readable even though the network work happened concurrently.
    """
    p1_log: Optional[list] = [] if debug_log is not None else None
    p2_log: Optional[list] = [] if debug_log is not None else None
    p1_results: list = []
    p2_results: list = []

    def _run_p1():
        p1_results.extend(_p1.discover(
            timeout_s=timeout_s,
            attempts=attempts,
            local_bind=local_bind,
            target_ip=target_ip,
            debug_log=p1_log,
        ))

    def _run_p2():
        p2_results.extend(_p2.discover(
            timeout_s=timeout_s,
            attempts=attempts,
            local_bind=local_bind,
            target_ip=target_ip,
            debug_log=p2_log,
        ))

    t1 = threading.Thread(target=_run_p1, daemon=True)
    t2 = threading.Thread(target=_run_p2, daemon=True)
    t1.start()
    t2.start()
    # Both protocols' inner loops bound by their own (timeout_s, attempts)
    # — joining for the same window plus 1s slack catches any thread that
    # took the long path through retries.
    deadline = timeout_s * (attempts + 1) + 1.0
    t1.join(timeout=deadline)
    t2.join(timeout=deadline)

    if debug_log is not None:
        debug_log.append("--- P1 discovery ---")
        if p1_log is not None:
            debug_log.extend(f"[P1] {line}" for line in p1_log)
        debug_log.append("--- P2 discovery ---")
        if p2_log is not None:
            debug_log.extend(f"[P2] {line}" for line in p2_log)

    by_mac: dict[str, DiscoveredRadio] = {}
    for info in p1_results:
        by_mac[info.mac] = _wrap_p1(info)
    # P2 entries overwrite P1 entries for the same MAC.
    for info in p2_results:
        by_mac[info.mac] = _wrap_p2(info)

    return list(by_mac.values())


__all__ = ["DiscoveredRadio", "discover_all"]
