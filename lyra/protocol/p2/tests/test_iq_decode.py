"""DDC IQ frame decode tests.

We feed `parse_ddc_iq_frame` synthetic frames built from known I/Q values
and verify the float-domain output is what we expect to within float
precision.
"""
from __future__ import annotations

import struct
import unittest

import numpy as np

from lyra.protocol.p2.packets import (
    DDC_IQ_FRAME_LEN_24BIT,
    parse_ddc_iq_frame,
)


def _i24_be(x: int) -> bytes:
    """Encode `x` as a 24-bit big-endian two's-complement byte triple."""
    if x < 0:
        x = (1 << 24) + x
    return bytes([(x >> 16) & 0xFF, (x >> 8) & 0xFF, x & 0xFF])


def _build_frame(samples: list[tuple[int, int]],
                 *, seq: int = 0, timestamp: int = 0,
                 bits_per_sample: int = 24) -> bytes:
    """Build a DDC IQ frame with the given list of (I_int24, Q_int24) values."""
    n = len(samples)
    payload = bytearray()
    for i_val, q_val in samples:
        payload += _i24_be(i_val)
        payload += _i24_be(q_val)
    header = bytearray(16)
    struct.pack_into(">I", header, 0, seq)
    struct.pack_into(">Q", header, 4, timestamp)
    struct.pack_into(">H", header, 12, bits_per_sample)
    struct.pack_into(">H", header, 14, n)
    return bytes(header) + bytes(payload)


class ParseIqFrameTest(unittest.TestCase):
    def test_canonical_frame_size_yields_238_samples(self) -> None:
        # Build a frame the same shape as a real radio frame: 238 zero samples.
        data = _build_frame([(0, 0)] * 238)
        self.assertEqual(len(data), DDC_IQ_FRAME_LEN_24BIT)
        frame = parse_ddc_iq_frame(data)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.samples_per_frame, 238)
        self.assertEqual(frame.samples.shape, (238,))
        self.assertEqual(frame.samples.dtype, np.complex64)
        self.assertTrue(np.all(frame.samples == 0))

    def test_seq_and_timestamp_extracted(self) -> None:
        data = _build_frame([(1, 2)], seq=0xDEADBEEF, timestamp=0x0011223344556677)
        frame = parse_ddc_iq_frame(data)
        assert frame is not None
        self.assertEqual(frame.seq, 0xDEADBEEF)
        self.assertEqual(frame.timestamp, 0x0011223344556677)

    def test_positive_and_negative_24bit_extremes(self) -> None:
        # +max = 0x7FFFFF → +1.0 - 1/2^23
        # -max = -0x800000 → -1.0
        samples = [(0x7FFFFF, 0), (-0x800000, 0)]
        data = _build_frame(samples)
        frame = parse_ddc_iq_frame(data)
        assert frame is not None
        self.assertAlmostEqual(frame.samples[0].real, 1.0 - 1.0 / (1 << 23), places=6)
        self.assertAlmostEqual(frame.samples[0].imag, 0.0, places=6)
        self.assertAlmostEqual(frame.samples[1].real, -1.0, places=6)

    def test_q_channel_decoded_independently_of_i(self) -> None:
        samples = [(0, 0x400000), (0x400000, 0)]
        data = _build_frame(samples)
        frame = parse_ddc_iq_frame(data)
        assert frame is not None
        self.assertAlmostEqual(frame.samples[0].real, 0.0, places=6)
        self.assertAlmostEqual(frame.samples[0].imag, 0.5, places=6)
        self.assertAlmostEqual(frame.samples[1].real, 0.5, places=6)
        self.assertAlmostEqual(frame.samples[1].imag, 0.0, places=6)

    def test_negative_quarter(self) -> None:
        # -0.25 = -2^21 = -0x200000 in 24-bit two's complement
        samples = [(-0x200000, -0x200000)]
        data = _build_frame(samples)
        frame = parse_ddc_iq_frame(data)
        assert frame is not None
        self.assertAlmostEqual(frame.samples[0].real, -0.25, places=6)
        self.assertAlmostEqual(frame.samples[0].imag, -0.25, places=6)

    def test_short_packet_returns_none(self) -> None:
        self.assertIsNone(parse_ddc_iq_frame(b"\x00" * 10))

    def test_truncated_payload_returns_none(self) -> None:
        # Header claims 100 samples but only ship 5.
        header = bytearray(16)
        struct.pack_into(">H", header, 12, 24)   # 24 bits per sample
        struct.pack_into(">H", header, 14, 100)  # claim 100 samples
        partial = bytes(header) + b"\x00" * (5 * 6)
        self.assertIsNone(parse_ddc_iq_frame(partial))

    def test_unsupported_bits_per_sample_returns_none(self) -> None:
        # 32-bit IQ would be valid in spec but v1 only handles 24-bit.
        data = _build_frame([(0, 0)] * 100, bits_per_sample=32)
        self.assertIsNone(parse_ddc_iq_frame(data))


if __name__ == "__main__":
    unittest.main()
