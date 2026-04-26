"""openHPSDR Protocol 2 board-ID lookup.

Source: openHPSDR Ethernet Protocol v4.4, Discovery Reply byte 11 ("Board
Type"). When a new ID is seen in the wild (e.g. Apache Brick II — not
listed in v4.4), add a row here.

The `family` field lets higher layers group radios for UI purposes
("show all my Apache rigs") without scattering board-ID literals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BoardSpec:
    """One row of the P2 board lookup table.

    `max_sample_rate_hz` is the highest single-DDC sample rate the radio
    is documented to support. Used as a UI cap; the radio will clamp
    further if it can't keep up.

    `n_adcs` matters because Apache rigs with two ADCs can drive two
    independent receivers (DDC0 from ADC0, DDC1 from ADC1) for diversity
    or wide-coverage scenarios. v1 doesn't expose multi-ADC, but the
    field is kept so the UI can show capability.

    `ddc_offset_for_rx1` is THE quirk that makes Apache bring-up
    confusing: Hermes / Atlas use DDC0 for RX1, but ANGELIA / ORION /
    ORION-MkII / SATURN reserve DDC0 / DDC1 internally and start
    user-facing receivers at DDC2. pi-hpsdr's `new_protocol.c`:

        if (device == ANGELIA || ORION || ORION2 || SATURN) ddc = 2;

    Get this wrong and the radio silently doesn't stream — the IQ
    pipeline arms a DDC slot the firmware never services.
    """
    id: int
    name: str          # spec wording, e.g. "SATURN (ANAN-G2)"
    short_name: str    # what UI shows, e.g. "ANAN-G2"
    family: str        # "Apache" / "HermesLite" / "Atlas"
    max_sample_rate_hz: int
    n_adcs: int
    ddc_offset_for_rx1: int = 0


BOARDS: dict[int, BoardSpec] = {
    0:  BoardSpec(0,  "Atlas",                   "Atlas",        "Atlas",        384_000, 4),
    1:  BoardSpec(1,  "HERMES (ANAN-10/100)",    "ANAN-10",      "Apache",       384_000, 1),
    2:  BoardSpec(2,  "HERMES (ANAN-10E/100B)",  "ANAN-10E",     "Apache",       384_000, 1),
    3:  BoardSpec(3,  "ANGELIA (ANAN-100D)",     "ANAN-100D",    "Apache",       384_000, 2, 2),
    4:  BoardSpec(4,  "ORION (ANAN-200D)",       "ANAN-200D",    "Apache",       384_000, 2, 2),
    5:  BoardSpec(5,  "ORION Mk II",             "ANAN-7000DLE", "Apache",       384_000, 2, 2),
    6:  BoardSpec(6,  "Hermes Lite",             "HL2",          "HermesLite",   384_000, 1),
    10: BoardSpec(10, "SATURN (ANAN-G2)",        "ANAN-G2",      "Apache",     1_536_000, 2, 2),
}


def lookup_board(board_id: int) -> Optional[BoardSpec]:
    """Return the BoardSpec for `board_id`, or None if unknown.

    Unknown IDs are NOT an error — Apache may ship hardware ahead of
    spec updates, and the Brick II row is expected to land here once
    we observe its real ID. The caller decides how to render an
    unknown board (we suggest "Unknown ANAN/Apache (id=N)").
    """
    return BOARDS.get(board_id)


def is_apache(board_id: int) -> bool:
    """True if the board is in the Apache family per the lookup table."""
    spec = BOARDS.get(board_id)
    return spec is not None and spec.family == "Apache"
