"""Encoder tests for General, DDC Specific, and High Priority packets.

We assert specific byte offsets/values rather than full byte equality
so a future protocol field addition won't churn every test.
"""
from __future__ import annotations

import struct
import unittest

from lyra.protocol.p2.packets import (
    DDC_SPECIFIC_PACKET_LEN,
    GENERAL_PACKET_LEN,
    HIGH_PRIORITY_PACKET_LEN,
    DdcConfig,
    GeneralPacketConfig,
    HighPriorityConfig,
    build_ddc_specific_packet,
    build_general_packet,
    build_high_priority_packet,
)


class GeneralPacketTest(unittest.TestCase):
    def test_size(self) -> None:
        pkt = build_general_packet(seq=42, cfg=GeneralPacketConfig())
        self.assertEqual(len(pkt), GENERAL_PACKET_LEN)
        self.assertEqual(len(pkt), 60)

    def test_seq_and_cmd_byte(self) -> None:
        pkt = build_general_packet(seq=0xDEADBEEF, cfg=GeneralPacketConfig())
        self.assertEqual(struct.unpack(">I", pkt[0:4])[0], 0xDEADBEEF)
        self.assertEqual(pkt[4], 0x00, "byte 4 distinguishes General from Discovery")

    def test_default_port_assignments(self) -> None:
        pkt = build_general_packet(seq=0, cfg=GeneralPacketConfig())
        self.assertEqual(struct.unpack(">H", pkt[5:7])[0], 1025)   # DDC cmd
        self.assertEqual(struct.unpack(">H", pkt[7:9])[0], 1026)   # DUC cmd
        self.assertEqual(struct.unpack(">H", pkt[9:11])[0], 1027)  # HP from PC
        self.assertEqual(struct.unpack(">H", pkt[11:13])[0], 1025) # HP to PC
        self.assertEqual(struct.unpack(">H", pkt[13:15])[0], 1028) # DDC audio
        self.assertEqual(struct.unpack(">H", pkt[15:17])[0], 1029) # DUC0 IQ
        self.assertEqual(struct.unpack(">H", pkt[17:19])[0], 1035) # DDC0 IQ

    def test_custom_iq_destination_port(self) -> None:
        cfg = GeneralPacketConfig(ddc_iq_destination_port=54321)
        pkt = build_general_packet(seq=0, cfg=cfg)
        self.assertEqual(struct.unpack(">H", pkt[17:19])[0], 54321)


class DdcSpecificTest(unittest.TestCase):
    def test_size(self) -> None:
        pkt = build_ddc_specific_packet(seq=0)
        self.assertEqual(len(pkt), DDC_SPECIFIC_PACKET_LEN)
        self.assertEqual(len(pkt), 1444)

    def test_seq_at_head(self) -> None:
        pkt = build_ddc_specific_packet(seq=0xCAFEBABE)
        self.assertEqual(struct.unpack(">I", pkt[0:4])[0], 0xCAFEBABE)

    def test_default_enables_only_ddc0(self) -> None:
        pkt = build_ddc_specific_packet(seq=0)
        # byte 7 bit 0 = DDC0 enable; rest of the bitmap should be zero.
        self.assertEqual(pkt[7], 0x01)
        self.assertEqual(pkt[8:17], bytes(9))

    def test_default_ddc0_block(self) -> None:
        pkt = build_ddc_specific_packet(seq=0)
        # DDC0 block at byte 17: ADC0, 48 ksps, CIC=0/0, 24-bit
        self.assertEqual(pkt[17], 0x00)                              # ADC0
        self.assertEqual(struct.unpack(">H", pkt[18:20])[0], 48)     # 48 ksps
        self.assertEqual(pkt[20], 0)                                 # CIC1
        self.assertEqual(pkt[21], 0)                                 # CIC2
        self.assertEqual(pkt[22], 24)                                # 24-bit

    def test_custom_rate_192k(self) -> None:
        pkt = build_ddc_specific_packet(
            seq=0,
            ddcs={0: DdcConfig(adc_source=0, sample_rate_hz=192_000, sample_size_bits=24)},
        )
        self.assertEqual(struct.unpack(">H", pkt[18:20])[0], 192)

    def test_custom_rate_1536k_g2(self) -> None:
        pkt = build_ddc_specific_packet(
            seq=0,
            ddcs={0: DdcConfig(adc_source=0, sample_rate_hz=1_536_000, sample_size_bits=24)},
        )
        self.assertEqual(struct.unpack(">H", pkt[18:20])[0], 1536)

    def test_invalid_rate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_ddc_specific_packet(
                seq=0,
                ddcs={0: DdcConfig(sample_rate_hz=37_000)},
            )

    def test_ddc_index_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            build_ddc_specific_packet(
                seq=0,
                ddcs={80: DdcConfig()},
            )

    def test_enable_mask_writes_correct_bytes(self) -> None:
        # Enable DDC0, DDC8, DDC15
        mask = (1 << 0) | (1 << 8) | (1 << 15)
        pkt = build_ddc_specific_packet(seq=0, ddc_enable_mask=mask, ddcs={0: DdcConfig()})
        self.assertEqual(pkt[7], 0x01)             # DDC0 → byte 7 bit 0
        self.assertEqual(pkt[8], 0x01 | 0x80)      # DDC8 (bit 0) + DDC15 (bit 7)


class HighPriorityTest(unittest.TestCase):
    def test_size(self) -> None:
        pkt = build_high_priority_packet(seq=0, cfg=HighPriorityConfig())
        self.assertEqual(len(pkt), HIGH_PRIORITY_PACKET_LEN)
        self.assertEqual(len(pkt), 1444)

    def test_seq_and_run_bit(self) -> None:
        pkt = build_high_priority_packet(seq=7, cfg=HighPriorityConfig(run=True))
        self.assertEqual(struct.unpack(">I", pkt[0:4])[0], 7)
        self.assertEqual(pkt[4] & 0x01, 0x01, "run bit must be set")

    def test_no_run_no_ptt(self) -> None:
        pkt = build_high_priority_packet(seq=0, cfg=HighPriorityConfig(run=False))
        self.assertEqual(pkt[4], 0x00)

    def test_ptt_bits(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(run=True, ptt=(True, False, True, False)),
        )
        # bit0=run, bit1=PTT0, bit2=PTT1, bit3=PTT2, bit4=PTT3.
        # PTT0=True → bit1; PTT2=True → bit3.
        self.assertEqual(pkt[4], 0x01 | 0x02 | 0x08)

    def test_ddc0_freq_at_offset_9(self) -> None:
        # Phase mode (Apache default) — wire value is the phase increment,
        # not raw Hz. 14.250 MHz → 0x1DB00000 at 122.88 MHz sample clock.
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(run=True, ddc_freqs_hz={0: 14_250_000}),
        )
        self.assertEqual(struct.unpack(">I", pkt[9:13])[0], 0x1DB00000)
        # DDC1 freq slot must remain zero.
        self.assertEqual(struct.unpack(">I", pkt[13:17])[0], 0)

    def test_ddc0_freq_raw_hz_when_phase_mode_off(self) -> None:
        # Hz mode (legacy / loopback) — wire value matches the input verbatim.
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(
                run=True, phase_mode=False, ddc_freqs_hz={0: 14_250_000},
            ),
        )
        self.assertEqual(struct.unpack(">I", pkt[9:13])[0], 14_250_000)

    def test_ddc1_freq_at_offset_13(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(
                run=True, phase_mode=False,
                ddc_freqs_hz={0: 7_200_000, 1: 14_074_000},
            ),
        )
        self.assertEqual(struct.unpack(">I", pkt[9:13])[0], 7_200_000)
        self.assertEqual(struct.unpack(">I", pkt[13:17])[0], 14_074_000)

    def test_oc_and_user_outputs(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(
                open_collector_outputs=0x42,
                user_outputs_db9=0x09,
                mercury_attenuator_20db=0x03,
            ),
        )
        self.assertEqual(pkt[1401], 0x42)
        self.assertEqual(pkt[1402], 0x09)
        self.assertEqual(pkt[1403], 0x03)

    def test_freq_out_of_range(self) -> None:
        # Phase mode rejects freqs above the radio sample clock (122.88 MHz).
        with self.assertRaises(ValueError):
            build_high_priority_packet(
                seq=0,
                cfg=HighPriorityConfig(ddc_freqs_hz={0: 200_000_000}),
            )

    def test_alex_words(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(
                run=True, alex0_word=0x01100010, alex1_word=0x01100002,
            ),
        )
        self.assertEqual(struct.unpack(">I", pkt[1428:1432])[0], 0x01100002)
        self.assertEqual(struct.unpack(">I", pkt[1432:1436])[0], 0x01100010)

    def test_adc_step_attenuators(self) -> None:
        # bytes 1442 = ADC1, 1443 = ADC0 — pi-hpsdr layout.
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(
                run=True, adc0_step_atten_db=12, adc1_step_atten_db=5,
            ),
        )
        self.assertEqual(pkt[1443], 12, "ADC0 step atten at byte 1443")
        self.assertEqual(pkt[1442], 5, "ADC1 step atten at byte 1442")

    def test_step_attenuator_clamps_above_31(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(run=True, adc0_step_atten_db=99),
        )
        self.assertEqual(pkt[1443], 31, "above-range value must clamp to 31 dB")

    def test_step_attenuator_clamps_below_0(self) -> None:
        pkt = build_high_priority_packet(
            seq=0,
            cfg=HighPriorityConfig(run=True, adc0_step_atten_db=-5),
        )
        self.assertEqual(pkt[1443], 0, "negative value must clamp to 0 dB")


class P2StreamLnaGainTest(unittest.TestCase):
    """`P2Stream.set_lna_gain_db` translates operator gain into the
    Apache step-attenuator scale. We test the translation alone here
    without bringing up the network sockets."""

    def _stub_stream(self):
        from lyra.protocol.p2.stream import P2Stream
        # Construct without start(); only the in-memory state is exercised.
        s = P2Stream("127.0.0.1", sample_rate=192000, board_id=10)
        return s

    def test_default_atten_is_zero(self):
        s = self._stub_stream()
        self.assertEqual(s._adc0_step_atten_db, 0)

    def test_gain_31_no_attenuation(self):
        s = self._stub_stream()
        s.set_lna_gain_db(31)
        self.assertEqual(s._adc0_step_atten_db, 0)

    def test_gain_19_default_mid_atten(self):
        # The HL2 default gain of 19 should give a sane mid-range Apache
        # attenuation — picking 12 here pins the linear formula.
        s = self._stub_stream()
        s.set_lna_gain_db(19)
        self.assertEqual(s._adc0_step_atten_db, 12)

    def test_gain_zero_max_attenuation(self):
        s = self._stub_stream()
        s.set_lna_gain_db(0)
        self.assertEqual(s._adc0_step_atten_db, 31)

    def test_gain_negative_clamps_to_max_atten(self):
        s = self._stub_stream()
        s.set_lna_gain_db(-12)
        self.assertEqual(s._adc0_step_atten_db, 31)

    def test_gain_above_31_clamps_to_zero_atten(self):
        s = self._stub_stream()
        s.set_lna_gain_db(48)
        self.assertEqual(s._adc0_step_atten_db, 0)

    def test_repeated_same_value_is_idempotent(self):
        # Same gain twice in a row: second call should be a no-op even
        # before hitting the wire (early-return inside the lock).
        s = self._stub_stream()
        s.set_lna_gain_db(19)
        s.set_lna_gain_db(19)
        self.assertEqual(s._adc0_step_atten_db, 12)


if __name__ == "__main__":
    unittest.main()
