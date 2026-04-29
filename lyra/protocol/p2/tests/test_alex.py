"""Tests for the ANAN-7000/8000 Alex word band-switching tables.

The bit values are hardware-register facts (Apache schematic);
the per-band thresholds match the canonical pi-hpsdr lookup. The
tests below pin both so future edits don't silently move a relay.
"""
from __future__ import annotations

import unittest

from lyra.protocol.p2 import alex


# Wire-captured value from a working ANAN-G2 reference session at
# 14.107 MHz: bytes 1432..1435 (Alex0) = 0x01100002. We use this as
# the ground-truth golden value for "20m on a properly-driven Apache."
ALEX0_GOLDEN_20M_14107 = 0x01100002


class RxBpfBitTest(unittest.TestCase):
    """Verify the RX BPF bit follows the pi-hpsdr lookup thresholds."""

    def test_below_1500_khz_is_bypass(self) -> None:
        for hz in (0, 100_000, 1_499_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_BYPASS)

    def test_160m_band(self) -> None:
        for hz in (1_500_000, 1_810_000, 2_099_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_160)

    def test_80_60m_band(self) -> None:
        for hz in (2_100_000, 3_650_000, 5_499_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_80_60)

    def test_40_30m_band(self) -> None:
        for hz in (5_500_000, 7_100_000, 10_106_000, 10_999_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_40_30)

    def test_20_15m_band(self) -> None:
        for hz in (11_000_000, 14_107_000, 18_068_000, 21_999_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_20_15)

    def test_12_10m_band(self) -> None:
        for hz in (22_000_000, 24_900_000, 28_500_000, 34_999_999):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_12_10)

    def test_6m_preamp(self) -> None:
        for hz in (35_000_000, 50_125_000, 54_000_000):
            self.assertEqual(alex.rx_bpf_bit_for(hz), alex.ALEX_RX_BPF_6_PRE)

    def test_threshold_boundaries(self) -> None:
        # The boundary value belongs to the upper band per `<` semantics.
        self.assertEqual(alex.rx_bpf_bit_for(1_500_000), alex.ALEX_RX_BPF_160)
        self.assertEqual(alex.rx_bpf_bit_for(2_100_000), alex.ALEX_RX_BPF_80_60)
        self.assertEqual(alex.rx_bpf_bit_for(5_500_000), alex.ALEX_RX_BPF_40_30)
        self.assertEqual(alex.rx_bpf_bit_for(11_000_000), alex.ALEX_RX_BPF_20_15)
        self.assertEqual(alex.rx_bpf_bit_for(22_000_000), alex.ALEX_RX_BPF_12_10)
        self.assertEqual(alex.rx_bpf_bit_for(35_000_000), alex.ALEX_RX_BPF_6_PRE)


class TxLpfBitTest(unittest.TestCase):
    """Verify the TX LPF bit follows the pi-hpsdr lookup thresholds."""

    def test_160m_lpf(self) -> None:
        for hz in (0, 1_810_000, 2_500_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_160)

    def test_80m_lpf(self) -> None:
        for hz in (2_500_001, 3_650_000, 5_000_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_80)

    def test_60_40m_lpf(self) -> None:
        for hz in (5_000_001, 7_100_000, 8_000_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_60_40)

    def test_30_20m_lpf(self) -> None:
        for hz in (8_000_001, 14_250_000, 16_500_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_30_20)

    def test_17_15m_lpf(self) -> None:
        for hz in (16_500_001, 21_300_000, 24_000_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_17_15)

    def test_12_10m_lpf(self) -> None:
        for hz in (24_000_001, 28_500_000, 35_600_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_12_10)

    def test_6m_bypass_lpf(self) -> None:
        for hz in (35_600_001, 50_125_000, 54_000_000):
            self.assertEqual(alex.tx_lpf_bit_for(hz), alex.ALEX_TX_LPF_6_BYPASS)


class Alex0WordTest(unittest.TestCase):
    """Composed Alex0 word: TX-ANT1 | TX-LPF | RX-BPF for current freq."""

    def test_20m_matches_wire_capture_golden(self) -> None:
        # Replays the wire-captured value from the Phase 6b ANAN-G2
        # session at 14.107 MHz. If this regresses, our band switching
        # is producing different bits than a known-working session.
        self.assertEqual(alex.alex0_word_for(14_107_000),
                         ALEX0_GOLDEN_20M_14107)

    def test_carries_tx_antenna_1(self) -> None:
        # Every Alex0 word should set the TX-ANT1 routing bit; that's
        # the v1 RX-only convention (the radio still routes RF through
        # the antenna selector even with no TX in flight).
        for hz in (1_810_000, 7_074_000, 14_250_000, 28_400_000):
            self.assertTrue(alex.alex0_word_for(hz) & alex.ALEX_TX_ANTENNA_1)

    def test_carries_correct_rx_bpf(self) -> None:
        word = alex.alex0_word_for(7_074_000)        # 40 m
        self.assertTrue(word & alex.ALEX_RX_BPF_40_30)

    def test_carries_correct_tx_lpf(self) -> None:
        word = alex.alex0_word_for(14_250_000)       # 20 m → 30/20 LPF
        self.assertTrue(word & alex.ALEX_TX_LPF_30_20)


class Alex1WordTest(unittest.TestCase):
    """Alex1 mirrors Alex0 minus the TX-antenna bit."""

    def test_does_not_set_tx_antenna(self) -> None:
        for hz in (1_810_000, 7_074_000, 14_250_000):
            self.assertFalse(alex.alex1_word_for(hz) & alex.ALEX_TX_ANTENNA_1)

    def test_carries_rx_bpf_and_tx_lpf(self) -> None:
        word = alex.alex1_word_for(14_107_000)
        self.assertTrue(word & alex.ALEX_RX_BPF_20_15)
        self.assertTrue(word & alex.ALEX_TX_LPF_30_20)


if __name__ == "__main__":
    unittest.main()
