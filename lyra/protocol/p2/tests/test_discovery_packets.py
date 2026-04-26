"""P2 discovery packet encode/decode tests.

These tests don't touch the network — they exercise `_build_discovery_packet`
and `_parse_reply` against synthetic byte strings constructed from the
spec.
"""
from __future__ import annotations

import unittest

from lyra.protocol.p2 import discovery as p2_disco


class BuildDiscoveryPacketTest(unittest.TestCase):
    def test_packet_is_60_bytes(self) -> None:
        pkt = p2_disco._build_discovery_packet()
        self.assertEqual(len(pkt), p2_disco.DISCOVERY_PACKET_LEN)
        self.assertEqual(len(pkt), 60)

    def test_seq_number_is_zero(self) -> None:
        pkt = p2_disco._build_discovery_packet()
        self.assertEqual(pkt[0:4], b"\x00\x00\x00\x00")

    def test_command_byte(self) -> None:
        pkt = p2_disco._build_discovery_packet()
        self.assertEqual(pkt[4], p2_disco.CMD_DISCOVERY)
        self.assertEqual(pkt[4], 0x02)

    def test_remainder_is_zero(self) -> None:
        pkt = p2_disco._build_discovery_packet()
        self.assertEqual(pkt[5:60], bytes(55))


def _build_synthetic_reply(
    *,
    status: int = p2_disco.STATUS_IDLE,
    mac: bytes = b"\xAA\xBB\xCC\xDD\xEE\xFF",
    board_id: int = 10,
    protocol_version: int = 104,
    code_version: int = 17,
    metis_version: int = 5,
    num_ddcs: int = 8,
    freq_or_phase: int = 0,
    endian_modes: int = 0x01,
    is_beta: int = 0,
) -> bytes:
    """Build a 60-byte P2 discovery reply per the v4.4 spec layout."""
    pkt = bytearray(60)
    # bytes 0..3: seq# = 0
    pkt[4] = status
    pkt[5:11] = mac
    pkt[11] = board_id
    pkt[12] = protocol_version
    pkt[13] = code_version
    # 14..17 mercury 0..3, 18 penny — leave zero
    pkt[19] = metis_version
    pkt[20] = num_ddcs
    pkt[21] = freq_or_phase
    pkt[22] = endian_modes
    pkt[23] = is_beta
    return bytes(pkt)


class ParseReplyTest(unittest.TestCase):
    def test_anan_g2_idle(self) -> None:
        data = _build_synthetic_reply(status=0x02, board_id=10)
        info = p2_disco._parse_reply(data, "192.168.1.50")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.ip, "192.168.1.50")
        self.assertEqual(info.mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(info.board_id, 10)
        self.assertEqual(info.board_name, "ANAN-G2")
        self.assertEqual(info.protocol_version, 104)
        self.assertEqual(info.code_version, 17)
        self.assertEqual(info.num_ddcs, 8)
        self.assertFalse(info.is_busy)
        self.assertFalse(info.long_form)

    def test_anan_g2_busy(self) -> None:
        data = _build_synthetic_reply(status=0x03, board_id=10)
        info = p2_disco._parse_reply(data, "192.168.1.50")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.is_busy)

    def test_unknown_board_id_renders_friendly_name(self) -> None:
        data = _build_synthetic_reply(board_id=99)
        info = p2_disco._parse_reply(data, "10.0.0.1")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.board_id, 99)
        self.assertIn("Unknown", info.board_name)
        self.assertIn("99", info.board_name)

    def test_long_form_flag(self) -> None:
        data = _build_synthetic_reply(status=0xFE, board_id=10)
        info = p2_disco._parse_reply(data, "10.0.0.2")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.long_form)
        self.assertFalse(info.is_busy)

        data = _build_synthetic_reply(status=0xFF, board_id=10)
        info = p2_disco._parse_reply(data, "10.0.0.2")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.long_form)
        self.assertTrue(info.is_busy)

    def test_p1_reply_is_rejected(self) -> None:
        # P1 reply starts with EF FE 02 — must NOT be parsed as P2.
        p1_reply = bytes([0xEF, 0xFE, 0x02]) + bytes(57)
        info = p2_disco._parse_reply(p1_reply, "10.0.0.3")
        self.assertIsNone(info)

    def test_garbage_short_packet_is_rejected(self) -> None:
        self.assertIsNone(p2_disco._parse_reply(b"hello", "10.0.0.4"))

    def test_unknown_status_byte_is_rejected(self) -> None:
        # status byte 0x07 isn't defined; should be ignored as not-a-reply.
        data = _build_synthetic_reply(status=0x07, board_id=10)
        self.assertIsNone(p2_disco._parse_reply(data, "10.0.0.5"))

    def test_raw_reply_is_captured(self) -> None:
        data = _build_synthetic_reply(board_id=10)
        info = p2_disco._parse_reply(data, "10.0.0.6")
        assert info is not None
        self.assertEqual(info.raw_reply, data)
        self.assertEqual(len(info.raw_reply), 60)


if __name__ == "__main__":
    unittest.main()
