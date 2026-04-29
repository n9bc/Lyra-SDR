"""Apache ANAN-7000 / 8000 / G2 (ORION2-class) Alex word band switching.

The Alex0 and Alex1 control words in the openHPSDR Protocol 2 High
Priority packet (bytes 1432..1435 and 1428..1431, big-endian u32 each)
drive the physical preselector relays on the Apache rig's two filter
banks. Alex0 controls RX1's bandpass filter and the shared TX low-pass
filter; Alex1 controls RX2's bandpass filter (same TX LPF mirrored).

Bit values are register addresses on the Apache hardware — facts from
the published Apache schematic / openHPSDR Protocol 2 spec, not any
client's expression. The per-band frequency thresholds match the
canonical pi-hpsdr (DL1YCF, GPL-3.0) reference client; the table is
re-implemented from scratch in Python and contains no ported code.

Pre-Apache (Hermes / Atlas / Hermes-Lite) boards use a different bit
layout for the same bytes — those are NOT covered here. Callers should
gate use of this module on a board having `ddc_offset_for_rx1 >= 2`
(the same gate the rest of the P2 code uses to detect ORION2-class).

Note: in v1 we drive Alex words even for RX-only sessions because the
ORION2 input chain is otherwise muted. Per-band TX LPF bits land in
the same word; they're set to match the RX frequency too because the
firmware uses them as a side-band attenuator hint regardless of TX
state.
"""
from __future__ import annotations


# ─── ANAN-7000/8000 RX bandpass filter bits (Alex0 / Alex1, lower 16 bits) ───

ALEX_RX_BPF_BYPASS  = 0x00001000     # bit 12
ALEX_RX_BPF_160     = 0x00000040     # bit  6,  1.5 –  2.0 MHz
ALEX_RX_BPF_80_60   = 0x00000020     # bit  5,  2.1 –  5.4 MHz
ALEX_RX_BPF_40_30   = 0x00000010     # bit  4,  5.5 – 10.9 MHz
ALEX_RX_BPF_20_15   = 0x00000002     # bit  1, 11.0 – 22.0 MHz
ALEX_RX_BPF_12_10   = 0x00000004     # bit  2, 22.0 – 35.6 MHz
ALEX_RX_BPF_6_PRE   = 0x00000008     # bit  3,  > 35.6 MHz with preamp


# ─── TX low-pass filter bits (shared via Alex0 word, upper bits) ────────────

ALEX_TX_LPF_160        = 0x00800000  # bit 23,    ≤ 2.5 MHz
ALEX_TX_LPF_80         = 0x00400000  # bit 22,  2.5 – 5.0 MHz
ALEX_TX_LPF_60_40      = 0x00200000  # bit 21,  5.0 – 8.0 MHz
ALEX_TX_LPF_30_20      = 0x00100000  # bit 20,  8.0 – 16.5 MHz
ALEX_TX_LPF_17_15      = 0x80000000  # bit 31, 16.5 – 24.0 MHz
ALEX_TX_LPF_12_10      = 0x40000000  # bit 30, 24.0 – 35.6 MHz
ALEX_TX_LPF_6_BYPASS   = 0x20000000  # bit 29,    > 35.6 MHz


# ─── TX antenna routing (we hard-code ANT1 in v1 RX-only) ───────────────────

ALEX_TX_ANTENNA_1      = 0x01000000  # bit 24


def rx_bpf_bit_for(freq_hz: int) -> int:
    """Return the ANAN-7000/8000 RX BPF bit for `freq_hz`.

    Thresholds are the open-bracket upper edges (`<`), matching the
    pi-hpsdr lookup. Below 1.5 MHz the bypass filter is engaged; above
    35 MHz the 6 m preamp path is engaged.
    """
    if freq_hz < 1_500_000:
        return ALEX_RX_BPF_BYPASS
    if freq_hz < 2_100_000:
        return ALEX_RX_BPF_160
    if freq_hz < 5_500_000:
        return ALEX_RX_BPF_80_60
    if freq_hz < 11_000_000:
        return ALEX_RX_BPF_40_30
    if freq_hz < 22_000_000:
        return ALEX_RX_BPF_20_15
    if freq_hz < 35_000_000:
        return ALEX_RX_BPF_12_10
    return ALEX_RX_BPF_6_PRE


def tx_lpf_bit_for(freq_hz: int) -> int:
    """Return the TX LPF bit for `freq_hz`.

    Thresholds are the closed-bracket lower edges (`>`); the pi-hpsdr
    lookup uses these exact splits. Returned bit lives in the Alex0
    word (the TX LPF is shared across both Alex banks).
    """
    if freq_hz > 35_600_000:
        return ALEX_TX_LPF_6_BYPASS
    if freq_hz > 24_000_000:
        return ALEX_TX_LPF_12_10
    if freq_hz > 16_500_000:
        return ALEX_TX_LPF_17_15
    if freq_hz > 8_000_000:
        return ALEX_TX_LPF_30_20
    if freq_hz > 5_000_000:
        return ALEX_TX_LPF_60_40
    if freq_hz > 2_500_000:
        return ALEX_TX_LPF_80
    return ALEX_TX_LPF_160


def alex0_word_for(rx1_freq_hz: int) -> int:
    """Compose the Alex0 control word for an ORION2-class board.

    Combines the RX1 BPF for the current tune frequency, the matching
    TX LPF, and TX-antenna-1 routing. RX-only operation still needs
    these bits set; the input chain is muted otherwise.
    """
    return (ALEX_TX_ANTENNA_1
            | tx_lpf_bit_for(rx1_freq_hz)
            | rx_bpf_bit_for(rx1_freq_hz))


def alex1_word_for(rx2_freq_hz: int) -> int:
    """Compose the Alex1 control word.

    In v1 we don't drive RX2 separately, so callers pass the RX1
    frequency and the Alex1 word mirrors Alex0 (minus the TX-antenna
    bit, which is Alex0-only). This keeps the second-ADC input
    ungrounded so the radio doesn't apply unwanted attenuation.
    """
    return tx_lpf_bit_for(rx2_freq_hz) | rx_bpf_bit_for(rx2_freq_hz)
