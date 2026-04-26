"""openHPSDR Protocol 2 packet encoders + IQ-frame decoder.

Spec: openHPSDR Ethernet Protocol v4.4 (Mar 2019).

Encoders:
    build_general_packet         — host→radio:1024, byte[4]=0x00. Set after
                                   discovery to declare port assignments,
                                   endian mode, wideband config.
    build_ddc_specific_packet    — host→radio:1025. Per-DDC sample rate,
                                   ADC source, sample size, enable bitmap.
    build_high_priority_packet   — host→radio:1027. Per-DDC RX frequency,
                                   run/PTT bits, OC pins, attenuators.

Decoder:
    parse_ddc_iq_frame           — radio→host on DDCn port (DDC0 default 1035).
                                   Returns (seq, timestamp, samples) where
                                   samples is complex64 normalized to [-1, 1).

All multibyte integers on the wire are big-endian unless the General Packet
specifically requests little-endian via the Endian-mode byte.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

import numpy as np


# ─── port defaults (post-discovery; can be overridden via General Packet) ─────

DEFAULT_DISCOVERY_PORT = 1024
DEFAULT_DDC_COMMAND_PORT = 1025
DEFAULT_DUC_COMMAND_PORT = 1026
DEFAULT_HIGH_PRIORITY_HOST_PORT = 1027   # host → radio
DEFAULT_HIGH_PRIORITY_RADIO_PORT = 1025  # radio → host (status)
DEFAULT_DDC_AUDIO_PORT = 1028            # host → radio (radio's own analog out)
DEFAULT_DUC_IQ_BASE_PORT = 1029          # host → radio (TX baseband)
DEFAULT_DDC_IQ_BASE_PORT = 1035          # radio → host (RX baseband)
DEFAULT_MIC_PORT = 1026                  # radio → host (mic samples)
DEFAULT_WIDEBAND_BASE_PORT = 1027        # radio → host (wideband ADC dump)


# ─── packet sizes ─────────────────────────────────────────────────────────────

GENERAL_PACKET_LEN = 60
DDC_SPECIFIC_PACKET_LEN = 1444    # spec uses up to byte 1443 (currently not used)
HIGH_PRIORITY_PACKET_LEN = 1444   # ditto — covers all defined fields + Alex slots
DDC_IQ_FRAME_LEN_24BIT = 1444     # 16 header + 238*(3+3) sample bytes
DUC_SPECIFIC_PACKET_LEN = 60      # spec: 60-byte fixed-size DUC config


# ─── DDC/DUC frequency encoding (phase increment) ─────────────────────────────
#
# Apache firmware (verified against ANAN-G2 wire capture + pi-hpsdr source)
# expects DDC and DUC frequencies as 32-bit phase-accumulator increments,
# NOT raw Hz. The constant `(1 << 32) / 122_880_000` is the per-Hz phase
# step at the 122.88 MHz radio sample clock — pi-hpsdr's
# `new_protocol.c` uses this exact value (34.952533...).
#
# The radio also requires General-Packet byte 37 = 0x08 to actually
# interpret these fields as phase increments. Without that flag the
# FPGA falls back to a Hz interpretation and tunes nowhere useful.

RADIO_SAMPLE_CLOCK_HZ = 122_880_000
PHASE_PER_HZ = (1 << 32) / RADIO_SAMPLE_CLOCK_HZ   # ≈ 34.95253...


def freq_hz_to_phase(freq_hz: int) -> int:
    """Convert a tune frequency in Hz to the 32-bit phase increment."""
    if not 0 <= freq_hz <= RADIO_SAMPLE_CLOCK_HZ:
        raise ValueError(
            f"freq_hz {freq_hz} outside [0, {RADIO_SAMPLE_CLOCK_HZ}] — "
            f"phase increment would overflow u32"
        )
    return int(round(freq_hz * PHASE_PER_HZ)) & 0xFFFFFFFF


def phase_to_freq_hz(phase: int) -> int:
    """Inverse of `freq_hz_to_phase` — useful for decoding captures + loopback."""
    return int(round(phase / PHASE_PER_HZ))


# ─── valid DDC sample rates (ksps) ────────────────────────────────────────────

VALID_DDC_RATES_KHZ = (48, 96, 192, 384, 768, 1536)


def _validate_rate(sample_rate_hz: int) -> int:
    """Convert a Hz sample rate to its ksps wire value, or raise."""
    rate_khz = sample_rate_hz // 1000
    if rate_khz * 1000 != sample_rate_hz:
        raise ValueError(f"sample_rate_hz must be a kHz multiple, got {sample_rate_hz}")
    if rate_khz not in VALID_DDC_RATES_KHZ:
        raise ValueError(
            f"sample_rate_hz must yield one of {VALID_DDC_RATES_KHZ} kHz, "
            f"got {rate_khz} kHz"
        )
    return rate_khz


# ─── General Packet (host → radio:1024) ──────────────────────────────────────

@dataclass
class GeneralPacketConfig:
    """All settings carried in a General Packet to the SDR.

    Defaults match the spec defaults — sending a GeneralPacketConfig() and
    encoding it produces a packet that asks the radio to use the standard
    port plan, big-endian wire format, and 24-bit DDC IQ samples.

    `ddc_iq_destination_port` is the only field most callers will set: it
    tells the radio what host UDP port to send DDC0 IQ data to (and DDC1
    will go to base+1, DDC2 to base+2, etc.).
    """
    ddc_command_port: int = DEFAULT_DDC_COMMAND_PORT
    duc_command_port: int = DEFAULT_DUC_COMMAND_PORT
    high_priority_from_pc_port: int = DEFAULT_HIGH_PRIORITY_HOST_PORT
    high_priority_to_pc_port: int = DEFAULT_HIGH_PRIORITY_RADIO_PORT
    ddc_audio_port: int = DEFAULT_DDC_AUDIO_PORT
    duc0_iq_port: int = DEFAULT_DUC_IQ_BASE_PORT
    ddc_iq_destination_port: int = DEFAULT_DDC_IQ_BASE_PORT  # DDC0 base
    mic_samples_port: int = DEFAULT_MIC_PORT
    wideband_base_port: int = DEFAULT_WIDEBAND_BASE_PORT
    wideband_enable_mask: int = 0x00          # bits[7:0] = WB0..WB7 enable
    wideband_samples_per_packet: int = 512
    wideband_sample_size_bits: int = 16
    wideband_update_rate_ms: int = 20
    wideband_packets_per_frame: int = 32
    enable_hardware_timer: bool = True
    # Endian / data-format byte 39, default 0 = big-endian + 3-byte IQ.
    # Bit layouts per spec:
    #   bit 0 = 1 → little-endian wire format
    #   bit 1 = 1 → 2's complement (vs. offset binary) — usually 1
    #   bit 2 = 1 → use 3-byte format (default; alternatives are float/double)
    #   bit 3 = 1 → use float
    #   bit 4 = 1 → use double
    endian_and_iq_format: int = 0x00
    # Byte 37 = "freq-mode bits". Setting bit 3 (0x08) tells the radio
    # that DDC/DUC frequencies in the High Priority packet are 32-bit
    # phase increments rather than raw Hz. Apache firmware (incl. v4.3
    # on ANAN-G2) requires this for any HF tune to work; pi-hpsdr always
    # sets it. Default True — change only if you know your radio expects
    # raw-Hz mode (none observed in the wild).
    phase_mode: bool = True
    # Byte 58 = PA/Apollo bits. Bit 0 (0x01) enables the on-board PA
    # routing. ORION2 / SATURN need this set even for RX-only or the
    # input chain stays disabled. Bit 1 (0x02) is the Apollo tuner —
    # leave off unless you actually have an Apollo board.
    pa_enable: bool = True
    apollo_tuner: bool = False
    # Byte 59 = Alex enable bits. 0x01 = Alex0 only (Hermes), 0x03 =
    # Alex0 + Alex1 (ORION2 / SATURN — both filter banks active).
    # Without 0x03 on a dual-Alex radio, RF goes through the wrong
    # filter bank and audio is heavily attenuated.
    alex_enable: int = 0x03


def build_general_packet(seq: int, cfg: GeneralPacketConfig) -> bytes:
    """Encode a 60-byte General Packet. Byte[4]=0x00 distinguishes from discovery."""
    pkt = bytearray(GENERAL_PACKET_LEN)
    struct.pack_into(">I", pkt, 0, seq & 0xFFFFFFFF)
    pkt[4] = 0x00
    struct.pack_into(">H", pkt, 5,  cfg.ddc_command_port)
    struct.pack_into(">H", pkt, 7,  cfg.duc_command_port)
    struct.pack_into(">H", pkt, 9,  cfg.high_priority_from_pc_port)
    struct.pack_into(">H", pkt, 11, cfg.high_priority_to_pc_port)
    struct.pack_into(">H", pkt, 13, cfg.ddc_audio_port)
    struct.pack_into(">H", pkt, 15, cfg.duc0_iq_port)
    struct.pack_into(">H", pkt, 17, cfg.ddc_iq_destination_port)
    struct.pack_into(">H", pkt, 19, cfg.mic_samples_port)
    struct.pack_into(">H", pkt, 21, cfg.wideband_base_port)
    pkt[23] = cfg.wideband_enable_mask & 0xFF
    struct.pack_into(">H", pkt, 24, cfg.wideband_samples_per_packet)
    pkt[26] = cfg.wideband_sample_size_bits & 0xFF
    pkt[27] = cfg.wideband_update_rate_ms & 0xFF
    pkt[28] = cfg.wideband_packets_per_frame & 0xFF
    # 29..36 reserved (memory mapped / envelope PWM dot clock — leave zero;
    # Thetis writes some bytes here but pi-hpsdr leaves them zero and
    # ANAN-G2 streams happily either way).
    pkt[37] = 0x08 if cfg.phase_mode else 0x00
    pkt[38] = 0x01 if cfg.enable_hardware_timer else 0x00
    pkt[39] = cfg.endian_and_iq_format & 0xFF
    # 40..57 reserved (zero).
    pkt[58] = (0x01 if cfg.pa_enable else 0x00) | (0x02 if cfg.apollo_tuner else 0x00)
    pkt[59] = cfg.alex_enable & 0xFF
    return bytes(pkt)


# ─── DDC Specific Packet (host → radio:1025) ─────────────────────────────────

@dataclass
class DdcConfig:
    """Per-DDC configuration block (6 wire bytes)."""
    adc_source: int = 0          # 0 = ADC0, 1 = ADC1, ...
    sample_rate_hz: int = 48_000  # one of VALID_DDC_RATES_KHZ * 1000
    sample_size_bits: int = 24    # FPGA currently fixed at 24
    cic1: int = 0                 # reserved for future use
    cic2: int = 0                 # reserved for future use


def build_ddc_specific_packet(
    seq: int,
    *,
    n_adcs: int = 1,
    dither_mask: int = 0x00,
    random_mask: int = 0x00,
    ddc_enable_mask: int = 0x01,    # 80-bit; only low byte (DDC0..7) used here
    ddcs: Optional[dict[int, DdcConfig]] = None,
) -> bytes:
    """Encode a DDC Specific packet.

    Args:
        seq: per-port sequence number (host-incremented).
        n_adcs: how many ADCs the host wants the radio to use (max 8).
        dither_mask: bit N set → ADC N has dither enabled.
        random_mask: bit N set → ADC N has whitening (random) enabled.
        ddc_enable_mask: 80-bit bitmap (we accept up to 64 bits via Python int).
            Bit 0 enables DDC0, bit 1 enables DDC1, ...
        ddcs: per-DDC config. Defaults to {0: DdcConfig()} if None. Only the
            DDCs listed are written into the packet; others stay zero (which
            on the radio side means "don't care; you didn't enable me").

    Returns 1444 bytes. Reserved/unused bytes are left zero.
    """
    if ddcs is None:
        ddcs = {0: DdcConfig()}

    pkt = bytearray(DDC_SPECIFIC_PACKET_LEN)
    struct.pack_into(">I", pkt, 0, seq & 0xFFFFFFFF)
    pkt[4] = n_adcs & 0xFF
    pkt[5] = dither_mask & 0xFF
    pkt[6] = random_mask & 0xFF

    # 80-bit DDC enable bitmap, MSB-first per DDC index in each byte.
    # Spec layout: byte 7 bit[0]=DDC0, bit[1]=DDC1, ..., bit[7]=DDC7.
    #              byte 8 bit[0]=DDC8, ..., bit[7]=DDC15.   ...etc.
    for ddc_idx in range(80):
        if (ddc_enable_mask >> ddc_idx) & 1:
            byte_off = 7 + (ddc_idx // 8)
            bit_off = ddc_idx % 8
            pkt[byte_off] |= (1 << bit_off)

    # Per-DDC blocks: byte 17 + (ddc_idx * 6).
    for ddc_idx, cfg in ddcs.items():
        if not 0 <= ddc_idx < 80:
            raise ValueError(f"DDC index out of range: {ddc_idx}")
        rate_khz = _validate_rate(cfg.sample_rate_hz)
        block_off = 17 + (ddc_idx * 6)
        pkt[block_off + 0] = cfg.adc_source & 0xFF
        struct.pack_into(">H", pkt, block_off + 1, rate_khz)
        pkt[block_off + 3] = cfg.cic1 & 0xFF
        pkt[block_off + 4] = cfg.cic2 & 0xFF
        pkt[block_off + 5] = cfg.sample_size_bits & 0xFF

    # Sync matrix at bytes 1363..1442 stays zero (no synchronous DDCs).
    return bytes(pkt)


# ─── High Priority From PC (host → radio:1027) ───────────────────────────────

@dataclass
class HighPriorityConfig:
    """Per-send state for the High Priority From PC packet.

    `run` must be True for the radio to stream; PTT bits remain False
    in the v1 RX-only path. DDC and DUC frequencies are specified in
    Hz; the builder converts to phase increments on the wire when
    `phase_mode=True` (the default — required by Apache firmware).

    Alex0 / Alex1 control words drive the BPF / LPF / antenna relays.
    On ORION2 / SATURN both words must be set to non-zero "open the
    receive path" values or the input is muted. The defaults here
    mirror what Thetis sends for an idle ANAN-G2 on 20 m; future
    revisions should derive these from the current frequency.
    """
    run: bool = False
    ptt: tuple[bool, bool, bool, bool] = (False, False, False, False)
    ddc_freqs_hz: dict[int, int] = None             # type: ignore[assignment]
    duc0_freq_hz: int = 0
    duc0_drive_level: int = 0
    open_collector_outputs: int = 0                 # bits 1..7
    user_outputs_db9: int = 0                       # bits 0..3
    mercury_attenuator_20db: int = 0                # bits 0..3
    alex_attenuator_db: int = 0                     # 0..31
    phase_mode: bool = True                         # Hz → phase on the wire
    alex0_word: int = 0                             # bytes 1432..1435 BE
    alex1_word: int = 0                             # bytes 1428..1431 BE

    def __post_init__(self) -> None:
        if self.ddc_freqs_hz is None:
            self.ddc_freqs_hz = {}


def build_high_priority_packet(seq: int, cfg: HighPriorityConfig) -> bytes:
    """Encode a High Priority From PC packet (1444 bytes, byte[4]=run/PTT bits).

    Field layout (post-Apache-G2 reverse engineering, matches pi-hpsdr's
    `new_protocol_high_priority`):
        bytes 9 + ddc_idx*4 .. +3   — DDC[ddc_idx] tune word, BE u32
                                       (phase increment when phase_mode=True,
                                       raw Hz otherwise)
        bytes 329..332              — DUC0 tune word, BE u32 (same rule)
        byte 345                    — DUC0 drive level
        byte 1401..1403             — open collector / user-output bits
        bytes 1428..1431            — Alex1 control word, BE
        bytes 1432..1435            — Alex0 control word, BE

    Spec recommends sending this on every state change AND periodically
    as a refresh; v1 P2Stream sends it once per second.
    """
    pkt = bytearray(HIGH_PRIORITY_PACKET_LEN)
    struct.pack_into(">I", pkt, 0, seq & 0xFFFFFFFF)

    run_bit = 0x01 if cfg.run else 0x00
    ptt_bits = 0
    for i, p in enumerate(cfg.ptt):
        if p:
            ptt_bits |= (1 << (1 + i))   # PTT0=bit1, PTT1=bit2, ...
    pkt[4] = run_bit | ptt_bits

    # CWX bytes 5..8 left zero (RX path does not assert CWX).

    for ddc_idx, freq_hz in cfg.ddc_freqs_hz.items():
        if not 0 <= ddc_idx < 80:
            raise ValueError(f"DDC index out of range: {ddc_idx}")
        wire_value = freq_hz_to_phase(freq_hz) if cfg.phase_mode else freq_hz
        if not 0 <= wire_value <= 0xFFFFFFFF:
            raise ValueError(f"DDC[{ddc_idx}] tune word overflow: {wire_value}")
        struct.pack_into(">I", pkt, 9 + ddc_idx * 4, wire_value)

    if cfg.duc0_freq_hz:
        duc_wire = (
            freq_hz_to_phase(cfg.duc0_freq_hz) if cfg.phase_mode else cfg.duc0_freq_hz
        )
        struct.pack_into(">I", pkt, 329, duc_wire & 0xFFFFFFFF)
    pkt[345] = cfg.duc0_drive_level & 0xFF

    # OC + user-output + Mercury attenuator bytes (1401..1403).
    pkt[1401] = cfg.open_collector_outputs & 0xFF
    pkt[1402] = cfg.user_outputs_db9 & 0xFF
    pkt[1403] = cfg.mercury_attenuator_20db & 0xFF

    # Alex relay control words (BE u32 each). pi-hpsdr layout:
    #   1428..1431 = Alex1, 1432..1435 = Alex0.
    if cfg.alex1_word:
        struct.pack_into(">I", pkt, 1428, cfg.alex1_word & 0xFFFFFFFF)
    if cfg.alex0_word:
        struct.pack_into(">I", pkt, 1432, cfg.alex0_word & 0xFFFFFFFF)

    return bytes(pkt)


# ─── DUC Specific Packet (host → radio:1026) ─────────────────────────────────
#
# pi-hpsdr sends this packet unconditionally at startup (no `if (tx)` guard).
# The Apache FPGA expects a DUC config to come up before it fully arms the
# streaming engine, so RX-only callers should still emit one with the
# safe defaults below. Layout matches `new_protocol_transmit_specific`.

@dataclass
class DucConfig:
    """Per-send state for the DUC Specific (60-byte) packet.

    Defaults are pi-hpsdr's "RX-only safe" values: 1 DAC, no CW,
    sidetone muted, keyer at 18 wpm / 50% weight / 316 ms hang.
    """
    n_dacs: int = 1
    cw_mode_flags: int = 0x00
    sidetone_volume: int = 0          # 0..127
    sidetone_freq_hz: int = 700
    keyer_speed_wpm: int = 18
    keyer_weight: int = 50            # percent
    keyer_hang_ms: int = 316
    rf_delay: int = 9
    keyer_ramp_width: int = 0xC0
    adc0_attenuation_db: int = 0
    adc1_attenuation_db: int = 0


def build_duc_specific_packet(seq: int, cfg: Optional[DucConfig] = None) -> bytes:
    """Encode the 60-byte DUC Specific packet (host → radio:1026)."""
    if cfg is None:
        cfg = DucConfig()
    pkt = bytearray(DUC_SPECIFIC_PACKET_LEN)
    struct.pack_into(">I", pkt, 0, seq & 0xFFFFFFFF)
    pkt[4]  = cfg.n_dacs & 0xFF
    pkt[5]  = cfg.cw_mode_flags & 0xFF
    pkt[6]  = cfg.sidetone_volume & 0x7F
    struct.pack_into(">H", pkt, 7, cfg.sidetone_freq_hz & 0xFFFF)
    pkt[9]  = cfg.keyer_speed_wpm & 0xFF
    pkt[10] = cfg.keyer_weight & 0xFF
    struct.pack_into(">H", pkt, 11, cfg.keyer_hang_ms & 0xFFFF)
    pkt[13] = cfg.rf_delay & 0xFF
    pkt[17] = cfg.keyer_ramp_width & 0xFF
    pkt[58] = cfg.adc1_attenuation_db & 0xFF
    pkt[59] = cfg.adc0_attenuation_db & 0xFF
    return bytes(pkt)


# ─── DDC IQ frame parser (radio → host on 1035 + ddc_idx) ────────────────────

@dataclass
class IqFrame:
    seq: int
    timestamp: int           # 64-bit sample-clock count (VITA-49)
    bits_per_sample: int     # currently always 24
    samples_per_frame: int   # 238 for 1 DDC × 24 bits
    samples: np.ndarray      # complex64, normalized [-1, 1)


def parse_ddc_iq_frame(data: bytes) -> Optional[IqFrame]:
    """Decode a single-DDC 24-bit DDC IQ frame, or return None if malformed.

    Spec: 16-byte header + N × 6 bytes of (I3, Q3). For the canonical
    1-DDC × 24-bit configuration N is 238 and the frame is 1444 bytes.
    We parse whatever count the radio reports in the header so other
    sizes (e.g. 32-bit samples) wouldn't silently corrupt — though we
    only support 24-bit decoding here.
    """
    if len(data) < 16:
        return None
    seq = struct.unpack_from(">I", data, 0)[0]
    timestamp = struct.unpack_from(">Q", data, 4)[0]
    bits_per_sample = struct.unpack_from(">H", data, 12)[0]
    samples_per_frame = struct.unpack_from(">H", data, 14)[0]

    if bits_per_sample != 24:
        # v1 only handles 24-bit. Future: branch to float/double decoders.
        return None

    expected_payload = samples_per_frame * 6
    if len(data) - 16 < expected_payload:
        return None

    # Decode N pairs of (I3, Q3) into complex64 in [-1, 1).
    payload = np.frombuffer(
        data[16:16 + expected_payload], dtype=np.uint8
    ).reshape(samples_per_frame, 6)
    i_raw = (
        (payload[:, 0].astype(np.int32) << 16)
        | (payload[:, 1].astype(np.int32) << 8)
        | payload[:, 2].astype(np.int32)
    )
    q_raw = (
        (payload[:, 3].astype(np.int32) << 16)
        | (payload[:, 4].astype(np.int32) << 8)
        | payload[:, 5].astype(np.int32)
    )
    # sign-extend 24-bit → 32-bit
    i_raw = np.where(i_raw & 0x800000, i_raw - 0x1000000, i_raw)
    q_raw = np.where(q_raw & 0x800000, q_raw - 0x1000000, q_raw)
    scale = 1.0 / (1 << 23)
    samples = (i_raw.astype(np.float32) * scale) + 1j * (q_raw.astype(np.float32) * scale)

    return IqFrame(
        seq=seq,
        timestamp=timestamp,
        bits_per_sample=bits_per_sample,
        samples_per_frame=samples_per_frame,
        samples=samples.astype(np.complex64),
    )


def parse_ddc_iq_frames(data: bytes, frame_size: int = DDC_IQ_FRAME_LEN_24BIT):
    """Yield each native IQ frame inside a (possibly NIC-coalesced) recv buffer.

    Windows' Receive Segment Coalescing and Linux UDP-GRO will sometimes
    deliver 2, 3, or 4 native 1444-byte IQ frames stacked back-to-back
    in a single ``recvfrom()`` return. Calling ``parse_ddc_iq_frame`` on
    the raw buffer would only decode the first frame and silently drop
    the rest — verified against the captured ANAN-G2 wire data, which
    showed 5776-byte recvs (= 4 × 1444).

    This walker slices the buffer at ``frame_size`` boundaries and
    yields one ``IqFrame`` per chunk. Trailing bytes that don't make
    up a full frame are discarded; that mirrors what ``parse_ddc_iq_frame``
    would have done anyway.
    """
    if frame_size <= 0:
        return
    for offset in range(0, len(data) - frame_size + 1, frame_size):
        frame = parse_ddc_iq_frame(data[offset:offset + frame_size])
        if frame is not None:
            yield frame
