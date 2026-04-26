"""Tests for the ANAN-G2 (ORION2 / SATURN) wire-format details:
phase-mode encoding, the DDC-2 slot offset, the General-Packet
"magic" bytes, the DUC Specific packet, and the multi-frame IQ
parser that handles NIC receive coalescing.

These cover the gap that real-hardware bring-up against
192.168.10.206 surfaced — see `docs/p2-anan-g2-findings.md`.
"""
from __future__ import annotations

import struct
import unittest

from lyra.protocol.p2.packets import (
    DDC_IQ_FRAME_LEN_24BIT,
    DUC_SPECIFIC_PACKET_LEN,
    DdcConfig,
    DucConfig,
    GeneralPacketConfig,
    HighPriorityConfig,
    PHASE_PER_HZ,
    RADIO_SAMPLE_CLOCK_HZ,
    build_ddc_specific_packet,
    build_duc_specific_packet,
    build_general_packet,
    build_high_priority_packet,
    freq_hz_to_phase,
    parse_ddc_iq_frame,
    parse_ddc_iq_frames,
    phase_to_freq_hz,
)


class PhaseConversionTest(unittest.TestCase):
    def test_round_trip_hf_freqs(self) -> None:
        for hz in (1_800_000, 7_200_000, 14_250_000, 28_500_000, 50_000_000):
            phase = freq_hz_to_phase(hz)
            self.assertGreater(phase, 0)
            self.assertLess(phase, 1 << 32)
            # Round-trip should be within ±1 Hz of input (pi-hpsdr quantization).
            self.assertLessEqual(abs(phase_to_freq_hz(phase) - hz), 1)

    def test_phase_for_14_250_mhz_matches_pihpsdr(self) -> None:
        # pi-hpsdr's constant 4294967296/122880000 ≈ 34.952533...
        # 14.250 MHz → 0x1DB00000 (498523589).
        self.assertEqual(freq_hz_to_phase(14_250_000), 0x1DB00000)

    def test_phase_constant_value(self) -> None:
        self.assertAlmostEqual(PHASE_PER_HZ, 4294967296 / RADIO_SAMPLE_CLOCK_HZ, places=10)
        self.assertAlmostEqual(PHASE_PER_HZ, 34.95253333333333, places=10)

    def test_negative_freq_rejected(self) -> None:
        with self.assertRaises(ValueError):
            freq_hz_to_phase(-1)

    def test_freq_above_sample_clock_rejected(self) -> None:
        with self.assertRaises(ValueError):
            freq_hz_to_phase(RADIO_SAMPLE_CLOCK_HZ + 1)


class GeneralPacketAnanG2Test(unittest.TestCase):
    """The Apache 'magic' bytes 37, 38, 58, 59 must be set for an
    ANAN-G2 to interpret the rest of the configuration."""

    def test_phase_mode_byte_default_on(self) -> None:
        pkt = build_general_packet(seq=0, cfg=GeneralPacketConfig())
        self.assertEqual(pkt[37], 0x08, "byte 37 = 0x08 enables phase-increment mode")

    def test_phase_mode_off_clears_byte_37(self) -> None:
        pkt = build_general_packet(seq=0, cfg=GeneralPacketConfig(phase_mode=False))
        self.assertEqual(pkt[37], 0x00)

    def test_hardware_timer_enabled_by_default(self) -> None:
        pkt = build_general_packet(seq=0, cfg=GeneralPacketConfig())
        self.assertEqual(pkt[38], 0x01)

    def test_pa_and_alex_bits_set_for_orion2(self) -> None:
        pkt = build_general_packet(seq=0, cfg=GeneralPacketConfig())
        self.assertEqual(pkt[58], 0x01, "PA enable bit must be set on ORION2")
        self.assertEqual(pkt[59], 0x03, "Alex0 + Alex1 enable for dual-Alex radio")

    def test_apollo_tuner_bit(self) -> None:
        pkt = build_general_packet(
            seq=0,
            cfg=GeneralPacketConfig(pa_enable=True, apollo_tuner=True),
        )
        self.assertEqual(pkt[58], 0x01 | 0x02)

    def test_alex_enable_overridable_for_hermes(self) -> None:
        # Single-Alex Hermes-class radios use 0x01.
        pkt = build_general_packet(
            seq=0, cfg=GeneralPacketConfig(alex_enable=0x01),
        )
        self.assertEqual(pkt[59], 0x01)


class DdcSpecificDdc2Test(unittest.TestCase):
    """When the caller targets DDC2 (RX1 on ORION2 / SATURN), the
    enable bit moves to byte 7 bit 2 and the per-DDC config block
    moves to bytes 29..34."""

    def test_ddc2_enable_bit(self) -> None:
        pkt = build_ddc_specific_packet(
            seq=0,
            n_adcs=2,
            ddc_enable_mask=(1 << 2),
            ddcs={2: DdcConfig(adc_source=0, sample_rate_hz=192_000)},
        )
        self.assertEqual(pkt[4], 2, "n_adcs reflects ANAN-G2's two ADCs")
        self.assertEqual(pkt[7], 0x04, "bit 2 set → DDC2 enabled")
        self.assertEqual(pkt[8:17], bytes(9), "no other DDC bits set")

    def test_ddc2_block_at_byte_29(self) -> None:
        pkt = build_ddc_specific_packet(
            seq=0,
            n_adcs=2,
            ddc_enable_mask=(1 << 2),
            ddcs={2: DdcConfig(adc_source=0, sample_rate_hz=192_000,
                               sample_size_bits=24)},
        )
        block = 17 + 2 * 6     # = 29
        self.assertEqual(pkt[block + 0], 0x00)                          # ADC0
        self.assertEqual(struct.unpack(">H", pkt[block + 1:block + 3])[0], 192)
        self.assertEqual(pkt[block + 5], 24)
        # DDC0 / DDC1 blocks must remain zero so the radio doesn't
        # latch leftover state for receivers we never enabled.
        self.assertEqual(pkt[17:29], bytes(12))


class HighPriorityPhaseEncodingTest(unittest.TestCase):
    """High Priority packets in phase mode (Apache default) write the
    phase increment into bytes 9 + ddc_idx*4."""

    def test_ddc2_freq_at_byte_17(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(run=True, ddc_freqs_hz={2: 14_250_000}),
        )
        # DDC2 frequency lives at bytes 17..20 (= 9 + 2*4).
        self.assertEqual(struct.unpack(">I", pkt[17:21])[0], 0x1DB00000)
        # DDC0 / DDC1 slots must be zero on a DDC2-only stream.
        self.assertEqual(struct.unpack(">I", pkt[9:13])[0], 0)
        self.assertEqual(struct.unpack(">I", pkt[13:17])[0], 0)

    def test_known_thetis_capture_value(self) -> None:
        # From the captured Thetis session against the real ANAN-G2:
        # DDC2 wire value 0x150E06F6 → ~10.106 MHz (30 m). Round-trip
        # the captured phase back to the freq Thetis was tuned to.
        captured_phase = 0x150E06F6
        freq = phase_to_freq_hz(captured_phase)
        self.assertAlmostEqual(freq, 10_106_301, delta=10)


class DucSpecificTest(unittest.TestCase):
    def test_size(self) -> None:
        pkt = build_duc_specific_packet(seq=0)
        self.assertEqual(len(pkt), DUC_SPECIFIC_PACKET_LEN)
        self.assertEqual(len(pkt), 60)

    def test_default_n_dacs_one(self) -> None:
        pkt = build_duc_specific_packet(seq=0)
        self.assertEqual(pkt[4], 1)

    def test_default_keyer_speed_18_wpm(self) -> None:
        pkt = build_duc_specific_packet(seq=0)
        self.assertEqual(pkt[9], 18)
        self.assertEqual(pkt[10], 50)   # weight percent

    def test_seq_at_head(self) -> None:
        pkt = build_duc_specific_packet(seq=0xFEEDFACE)
        self.assertEqual(struct.unpack(">I", pkt[0:4])[0], 0xFEEDFACE)

    def test_attenuators_at_58_59(self) -> None:
        pkt = build_duc_specific_packet(
            seq=0,
            cfg=DucConfig(adc0_attenuation_db=11, adc1_attenuation_db=22),
        )
        self.assertEqual(pkt[58], 22)
        self.assertEqual(pkt[59], 11)


class ParseIqFramesCoalescingTest(unittest.TestCase):
    """Windows RSC and Linux UDP-GRO concatenate up to 4 IQ frames in a
    single recvfrom(). The walker must split and yield all of them."""

    def _frame(self, seq: int) -> bytes:
        header = bytearray(16)
        struct.pack_into(">I", header, 0, seq)
        struct.pack_into(">H", header, 12, 24)
        struct.pack_into(">H", header, 14, 238)
        return bytes(header) + bytes(238 * 6)

    def test_single_frame_yields_one(self) -> None:
        buf = self._frame(seq=100)
        frames = list(parse_ddc_iq_frames(buf))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 100)

    def test_four_coalesced_frames(self) -> None:
        buf = b"".join(self._frame(seq=s) for s in (200, 201, 202, 203))
        self.assertEqual(len(buf), 4 * DDC_IQ_FRAME_LEN_24BIT)
        seqs = [f.seq for f in parse_ddc_iq_frames(buf)]
        self.assertEqual(seqs, [200, 201, 202, 203])

    def test_trailing_partial_frame_discarded(self) -> None:
        buf = self._frame(seq=300) + b"\x00" * 7
        frames = list(parse_ddc_iq_frames(buf))
        self.assertEqual(len(frames), 1)

    def test_empty_buffer_yields_nothing(self) -> None:
        self.assertEqual(list(parse_ddc_iq_frames(b"")), [])

    def test_zero_frame_size_yields_nothing(self) -> None:
        self.assertEqual(list(parse_ddc_iq_frames(self._frame(0), frame_size=0)), [])


if __name__ == "__main__":
    unittest.main()
