"""openHPSDR Protocol 2 implementation for Apache ANAN family + Brick II.

Spec reference: openHPSDR Ethernet Protocol v4.4 (TAPR/OpenHPSDR-Firmware,
Mar 2019). The HL2-focused Protocol 1 layer lives in
`lyra.protocol.discovery` / `lyra.protocol.stream` and is unaffected by
this package.

Sub-modules:
    boards    — board-ID lookup table (Atlas, Hermes, Orion, Saturn, ...)
    discovery — P2 discovery handshake (UDP 1024)
    packets   — General Packet, DDC Specific, High Priority encoders (Phase 2)
    stream    — P2Stream RX session (Phase 2)
"""
from __future__ import annotations

from lyra.protocol.p2.boards import BOARDS, BoardSpec, lookup_board
from lyra.protocol.p2.discovery import P2RadioInfo, discover

__all__ = [
    "BOARDS",
    "BoardSpec",
    "lookup_board",
    "P2RadioInfo",
    "discover",
]
